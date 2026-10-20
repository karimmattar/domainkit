"""Authentication classes, JWT keys, and where the middleware must sit."""

import asyncio
import sys
from datetime import timedelta
from typing import Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from starlette.requests import HTTPConnection

from domainkit.contrib.auth import (
    AuthUnitOfWork,
    BaseAuthentication,
    JWTAuthentication,
    TokenKind,
)
from domainkit.contrib.auth.authentication import (
    AuthenticationFailed,
    RefreshNotSupported,
    bearer_token,
    get_authenticators,
    load_authenticators,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management.base import run_async
from domainkit_tests.conftest import UseAuth

AUTHENTICATION = "domainkit.contrib.auth.authentication"
HERE = "domainkit_tests.test_auth_authentication"
NotAClass = object()


def connection(authorization: str | None) -> HTTPConnection:
    """A connection with an ``Authorization`` header, or none."""
    headers = (
        [] if authorization is None else [(b"authorization", authorization.encode())]
    )
    return HTTPConnection({"type": "http", "headers": headers})


def pem(private: Any) -> tuple[str, str]:  # noqa: ANN401 - a cryptography key.
    """The private and public keys in PEM."""
    secret = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return secret, public


def jwt_auth(use_auth: UseAuth, **config: Any) -> JWTAuthentication:  # noqa: ANN401
    """A ``JWTAuthentication`` configured with ``AUTH["JWT"]``."""
    use_auth(AUTHENTICATION_CLASSES=[f"{AUTHENTICATION}.JWTAuthentication"], JWT=config)
    (authenticator,) = get_authenticators()
    assert isinstance(authenticator, JWTAuthentication)
    return authenticator


def test_bearer_tokens_are_read_from_the_header() -> None:
    """Only ``Bearer`` (in any case), and only a token that is there."""
    assert bearer_token(connection("Bearer abc")) == "abc"
    assert bearer_token(connection("bearer  abc ")) == "abc"
    assert bearer_token(connection("BEARER abc")) == "abc"
    for header in (None, "Basic abc", "Bearer", "Bearer   ", "Token abc"):
        assert bearer_token(connection(header)) is None


def test_the_base_class_knows_no_one(use_auth: UseAuth) -> None:
    """It recognizes nothing, issues nothing, and refreshes nothing."""
    use_auth()
    base = BaseAuthentication()

    assert asyncio.run(base.authenticate(connection("Bearer x"))) is None
    assert base.challenge() == 'Bearer realm="api"'
    assert (
        base.challenge("invalid_token") == 'Bearer realm="api", error="invalid_token"'
    )
    with pytest.raises(ImproperlyConfigured) as issuing:
        asyncio.run(base.issue(None, None))  # type: ignore[arg-type]
    assert str(issuing.value) == (
        "BaseAuthentication issues no tokens; list a class that does first in "
        "AUTH.AUTHENTICATION_CLASSES."
    )
    with pytest.raises(RefreshNotSupported):
        asyncio.run(base.refresh(None, "x"))  # type: ignore[arg-type]
    failed = AuthenticationFailed("Nope.")
    assert (failed.status_code, failed.detail, failed.code) == (
        401,
        "Nope.",
        "authentication_failed",
    )


def test_authentication_classes_are_checked(use_auth: UseAuth) -> None:
    """Unknown paths and other objects stop startup."""
    with pytest.raises(ImproperlyConfigured, match=r"^AUTH\.AUTHENTICATION_CLASSES: "):
        use_auth(AUTHENTICATION_CLASSES=["nowhere.Auth"])
    with pytest.raises(ImproperlyConfigured) as other:
        use_auth(AUTHENTICATION_CLASSES=[f"{HERE}.NotAClass"])
    assert str(other.value) == (
        f"AUTH.AUTHENTICATION_CLASSES: '{HERE}.NotAClass' is not a BaseAuthentication."
    )
    use_auth()
    first = get_authenticators()
    assert get_authenticators() is first
    assert load_authenticators.cache_info().currsize >= 1


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({}, "AUTH.JWT.SIGNING_KEY is required."),
        (
            {"SIGNING_KEY": "k" * 31},
            "AUTH.JWT.SIGNING_KEY must have at least 32 bytes for HS256.",
        ),
        (
            {"SIGNING_KEY": "k" * 47, "ALGORITHM": "HS384"},
            "AUTH.JWT.SIGNING_KEY must have at least 48 bytes for HS384.",
        ),
        (
            {"SIGNING_KEY": "k" * 63, "ALGORITHM": "HS512"},
            "AUTH.JWT.SIGNING_KEY must have at least 64 bytes for HS512.",
        ),
        (
            {"SIGNING_KEY": "private", "ALGORITHM": "RS256"},
            "AUTH.JWT.VERIFYING_KEY (the public key) is required for RS256.",
        ),
        (
            {
                "SIGNING_KEY": "not a key",
                "VERIFYING_KEY": "nor this",
                "ALGORITHM": "ES256",
            },
            "AUTH.JWT: the keys do not sign and verify ES256 tokens (",
        ),
    ],
)
def test_jwt_keys_are_checked(
    use_auth: UseAuth, config: dict[str, Any], message: str
) -> None:
    """Missing, short, or broken keys stop startup."""
    with pytest.raises(ImproperlyConfigured) as refused:
        jwt_auth(use_auth, **config)
    assert str(refused.value).startswith(message)


def test_jwt_needs_pyjwt(use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the extra, startup says how to install it."""
    load_authenticators.cache_clear()  # Instances made by earlier tests had PyJWT.
    monkeypatch.setitem(sys.modules, "jwt", None)
    with pytest.raises(ImproperlyConfigured) as missing:
        jwt_auth(use_auth, SIGNING_KEY="k" * 32)
    assert str(missing.value) == (
        'JWTAuthentication needs PyJWT: pip install "domainkit[jwt]".'
    )


def test_jwt_keys_that_do_not_match_are_refused(use_auth: UseAuth) -> None:
    """The public key must verify what the private key signs."""
    secret, _ = pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    _, other = pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    with pytest.raises(ImproperlyConfigured) as mismatch:
        jwt_auth(use_auth, SIGNING_KEY=secret, VERIFYING_KEY=other, ALGORITHM="RS256")
    assert str(mismatch.value) == (
        "AUTH.JWT: the keys do not sign and verify RS256 tokens "
        "(InvalidSignatureError)."
    )


@pytest.mark.parametrize(
    ("algorithm", "key"),
    [
        (
            "RS256",
            lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
        ),
        (
            "PS384",
            lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
        ),
        ("ES256", lambda: ec.generate_private_key(ec.SECP256R1())),
        ("EdDSA", ed25519.Ed25519PrivateKey.generate),
    ],
)
def test_asymmetric_keys_sign_and_verify(
    use_auth: UseAuth,
    algorithm: str,
    key: Any,  # noqa: ANN401 - a key factory.
) -> None:
    """Private keys sign; the public key alone verifies."""
    secret, public = pem(key())
    authenticator = jwt_auth(
        use_auth, SIGNING_KEY=secret, VERIFYING_KEY=public, ALGORITHM=algorithm
    )
    user = type("U", (), {"id": uuid4()})()
    token = authenticator.encode(user, uuid4())

    claims = jwt.decode(token, public, algorithms=[algorithm])
    assert set(claims) == {"sub", "iat", "exp", "jti", "sid"}
    assert claims["exp"] - claims["iat"] == 300
    assert jwt.get_unverified_header(token)["alg"] == algorithm


def test_jwt_claims_are_required_and_checked(use_auth: UseAuth) -> None:
    """Missing or malformed claims, and other algorithms, are invalid."""
    authenticator = jwt_auth(
        use_auth, SIGNING_KEY="k" * 32, ISSUER="us", AUDIENCE="api"
    )
    user = type("U", (), {"id": uuid4()})()
    claims = jwt.decode(
        authenticator.encode(user, uuid4()),
        "k" * 32,
        algorithms=["HS256"],
        audience="api",
    )
    assert (claims["iss"], claims["aud"]) == ("us", "api")

    def refused(**changes: Any) -> str:  # noqa: ANN401 - claim values.
        token = jwt.encode({**claims, **changes}, "k" * 32, algorithm="HS256")
        with pytest.raises(AuthenticationFailed) as failure:
            asyncio.run(authenticator.authenticate(connection(f"Bearer {token}")))
        return str(failure.value.detail)

    for claim in ("sub", "sid", "iss", "aud", "exp", "iat"):
        stripped = {key: value for key, value in claims.items() if key != claim}
        token = jwt.encode(stripped, "k" * 32, algorithm="HS256")
        with pytest.raises(AuthenticationFailed):
            authenticator.decode(token)
    assert refused(sid="not-a-uuid") == "Invalid token."
    hs512 = jwt.encode(claims, "k" * 64, algorithm="HS512")
    with pytest.raises(AuthenticationFailed):
        authenticator.decode(hs512)
    assert asyncio.run(authenticator.authenticate(connection("Bearer a.b"))) is None
    assert asyncio.run(authenticator.authenticate(connection(None))) is None


def test_refresh_tokens_must_be_complete(auth_project: str, use_auth: UseAuth) -> None:
    """Access tokens, and refresh tokens without a family or an end, refuse."""
    authenticator = jwt_auth(use_auth, SIGNING_KEY="k" * 32)

    async def scenario() -> list[str]:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.create_user("alice")
            issued = [
                await uow.tokens.issue(user.id, ttl=timedelta(days=1)),
                await uow.tokens.issue(
                    user.id, kind=TokenKind.REFRESH, ttl=timedelta(days=1)
                ),
                await uow.tokens.issue(
                    user.id, kind=TokenKind.REFRESH, ttl=None, family=uuid4()
                ),
                await uow.tokens.issue(
                    user.id,
                    kind=TokenKind.REFRESH,
                    ttl=timedelta(seconds=-1),
                    family=uuid4(),
                ),
            ]
            await uow.commit()
        details = []
        for raw in issued:
            async with AuthUnitOfWork() as uow:
                with pytest.raises(AuthenticationFailed) as failure:
                    await authenticator.refresh(uow, raw)
                details.append(str(failure.value.detail))
        return details

    assert run_async(scenario()) == ["Invalid refresh token."] * 4


@pytest.mark.parametrize(
    ("algorithm", "length"), [("HS256", 32), ("HS384", 48), ("HS512", 64)]
)
def test_hmac_keys_of_the_hash_length_are_enough(
    use_auth: UseAuth, algorithm: str, length: int
) -> None:
    """RFC 7518's minimum is accepted, at startup and by a new instance."""
    jwt_auth(use_auth, SIGNING_KEY="k" * length, ALGORITHM=algorithm)
    authenticator = JWTAuthentication()

    assert authenticator.signing_key == authenticator.verifying_key == b"k" * length


def test_throttled_counts_are_remembered_within_a_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each count's first refusal in a period is audited; old ones are let go."""
    from fastapi import FastAPI
    from starlette.requests import Request

    from domainkit.contrib.auth import throttling

    monkeypatch.setattr(throttling, "MAX_REFUSALS", 1)
    request = Request({"type": "http", "headers": [], "app": FastAPI()})

    assert throttling.first_refusal(request, "a", 60)
    assert not throttling.first_refusal(request, "a", 60)
    assert throttling.first_refusal(request, "b", 60)
    assert throttling.first_refusal(request, "a", 60)  # Let go when "b" came.


def test_a_refusal_is_audited_again_once_its_period_is_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Within the period, once; a full period later, again."""
    from fastapi import FastAPI
    from starlette.requests import Request

    from domainkit import clock
    from domainkit.contrib.auth import throttling

    request = Request({"type": "http", "headers": [], "app": FastAPI()})
    readings = iter([1000.0, 1059.5, 1060.0])
    monkeypatch.setattr(clock, "monotonic", lambda: next(readings))

    assert throttling.first_refusal(request, "a", 60)
    assert not throttling.first_refusal(request, "a", 60)
    assert throttling.first_refusal(request, "a", 60)


def test_logins_from_unknown_addresses_are_throttled_together(
    use_auth: UseAuth,
) -> None:
    """Without a client address, attempts count under one key."""
    from fastapi import FastAPI
    from starlette.requests import Request

    from domainkit.contrib.auth.api import _login_keys, _throttle
    from domainkit.http.errors import APIError

    use_auth(LOGIN_RATE="1/minute")
    app = FastAPI()
    anonymous = Request({"type": "http", "headers": [], "app": app})

    assert _login_keys(anonymous, "alice")[0] == "login:ip:unknown"
    asyncio.run(_throttle(anonymous, *_login_keys(anonymous, "alice")))
    with pytest.raises(APIError, match="Too many login attempts"):
        asyncio.run(_throttle(anonymous, *_login_keys(anonymous, "bob")))
