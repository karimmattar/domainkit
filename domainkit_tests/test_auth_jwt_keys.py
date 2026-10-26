"""JSON Web Tokens name their key, and a key retires without logging anyone out.

Until 0.11 a token carried no ``kid``: replacing ``AUTH.JWT.SIGNING_KEY``
refused every outstanding access token at once, and nothing kept an old
key for verification, bounded or not (AUTH-10).
"""

import asyncio
import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import ValidationError
from starlette.requests import HTTPConnection

from domainkit.checks import CheckFailed
from domainkit.clock import freeze_time
from domainkit.contrib.auth import AuthenticationFailed, JWTAuthentication
from domainkit.contrib.auth.authentication import get_authenticators
from domainkit.contrib.auth.settings import JWTSettings
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import Api, UseAuth

AUTHENTICATION = "domainkit.contrib.auth.authentication"
OLD = "o" * 32
NEW = "n" * 32
START = "2026-11-02T09:00:00Z"
PEM_LIKE = f"-----BEGIN PUBLIC KEY-----\n{'A' * 64}\n-----END PUBLIC KEY-----\n"
"""Text that looks like a public key: PyJWT never takes it as an HMAC secret."""
# RFC 7638, 3.1: an RSA public key and its JWK thumbprint (SHA-256).
RFC_7638_N = (
    "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1"
    "L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0"
    "h4QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n9"
    "1CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1j"
    "F44-csFCur-kEgU8awapJzKnqDKgw"
)
RFC_7638_THUMBPRINT = "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def connection(token: str) -> HTTPConnection:
    """A connection that sends ``token`` as a bearer token."""
    header = (b"authorization", f"Bearer {token}".encode())
    return HTTPConnection({"type": "http", "headers": [header]})


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


def rsa_pem() -> tuple[str, str]:
    """A new RSA key pair in PEM."""
    return pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))


def jwt_auth(use_auth: UseAuth, **config: Any) -> JWTAuthentication:  # noqa: ANN401
    """A ``JWTAuthentication`` configured with ``AUTH["JWT"]``."""
    use_auth(AUTHENTICATION_CLASSES=[f"{AUTHENTICATION}.JWTAuthentication"], JWT=config)
    (authenticator,) = get_authenticators()
    assert isinstance(authenticator, JWTAuthentication)
    return authenticator


def user() -> Any:  # noqa: ANN401 - anything with an id.
    """Something with an id, as ``encode`` needs."""
    return type("U", (), {"id": uuid4()})()


def verified(token: str, key: str, algorithm: str = "HS256") -> dict[str, Any]:
    """The claims of ``token``, its signature checked by PyJWT, its times not.

    The clock is frozen ahead of the host's, which PyJWT reads.
    """
    claims: dict[str, Any] = jwt.decode(
        token,
        key,
        algorithms=[algorithm],
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )
    return claims


def kid(token: str) -> object:
    """The ``kid`` a token's header names, if any."""
    return jwt.get_unverified_header(token).get("kid")


def segment(data: dict[str, Any]) -> str:
    """One base64url JSON segment of a JWT."""
    return encoded(json.dumps(data).encode())


def encoded(data: bytes) -> str:
    """``data`` as a segment of a JWT: base64url, without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def refusal(authenticator: JWTAuthentication, token: str) -> str:
    """Why ``authenticator`` refuses ``token``."""
    with pytest.raises(AuthenticationFailed) as refused:
        authenticator.decode(token)
    return str(refused.value.detail)


def at(moment: str, minutes: float = 0) -> datetime:
    """``moment`` plus ``minutes``, aware."""
    return datetime.fromisoformat(moment) + timedelta(minutes=minutes)


# The proof (AUTH-10) ---------------------------------------------------------


def test_tokens_name_the_key_that_signed_them(use_auth: UseAuth) -> None:
    """Every issued token's header carries ``kid``; nothing in it shows the key.

    An HMAC key's id is a keyed hash of a fixed label under the key itself,
    truncated: it says nothing a signature does not already say.
    """
    authenticator = jwt_auth(use_auth, SIGNING_KEY=OLD)
    token = authenticator.encode(user(), uuid4())
    other = jwt_auth(use_auth, SIGNING_KEY=NEW).encode(user(), uuid4())
    label = hmac.new(OLD.encode(), b"domainkit:jwt-key-id", hashlib.sha256).digest()

    assert kid(token) == base64.urlsafe_b64encode(label[:16]).rstrip(b"=").decode()
    assert kid(token) == authenticator.key_id
    assert kid(other) != kid(token)
    assert OLD not in json.dumps(jwt.get_unverified_header(token))
    assert kid(jwt_auth(use_auth, SIGNING_KEY=OLD).encode(user(), uuid4())) == kid(
        token
    )


def test_rotating_the_key_keeps_its_tokens_until_it_retires(use_auth: UseAuth) -> None:
    """A previous key verifies what it signed for one token lifetime after it retires.

    Without it, as before 0.11, replacing the key refuses every outstanding
    token at once. With it, tokens it signed work until its ``RETIRED_AT``
    plus ``ACCESS_TOKEN_TTL`` and ``LEEWAY``, then never again, even
    unexpired; the new key signs everything new.
    """
    ttl = timedelta(minutes=10)
    with freeze_time(START) as clock:
        old = jwt_auth(use_auth, SIGNING_KEY=OLD, ACCESS_TOKEN_TTL=ttl)
        token = old.encode(user(), uuid4())  # From a replica not yet rolled out.
        replaced = jwt_auth(use_auth, SIGNING_KEY=NEW, ACCESS_TOKEN_TTL=ttl)
        assert refusal(replaced, token) == "Invalid token."  # All at once.

        retired = at(START, -2)  # The rollout ended two minutes before.
        rotated = jwt_auth(
            use_auth,
            SIGNING_KEY=NEW,
            ACCESS_TOKEN_TTL=ttl,
            LEEWAY=timedelta(seconds=30),
            PREVIOUS_KEYS=[{"KEY": OLD, "RETIRED_AT": retired}],
        )
        fresh = rotated.encode(user(), uuid4())
        accepted = [rotated.decode(token)["sub"]]
        clock.move_to(at(START, 8.4))  # Retired + TTL + LEEWAY, less a breath.
        accepted.append(rotated.decode(token)["sub"])
        clock.move_to(at(START, 8.5))
        refused = refusal(rotated, token)
        valid = rotated.decode(fresh)

    assert len(accepted) == 2
    assert refused == "Invalid token."  # Its exp, 09:10, is still ahead.
    assert kid(fresh) == rotated.key_id == replaced.key_id
    assert kid(token) == old.key_id != rotated.key_id
    assert valid["sub"] == verified(fresh, NEW)["sub"]


def test_an_asymmetric_key_retires_into_another_algorithm(use_auth: UseAuth) -> None:
    """RS256 to ES256: each key verifies with its own algorithm, kept by its id.

    A previous asymmetric key is its public key alone; its id is its JWK
    thumbprint (RFC 7638), which any service holding the public key can
    compute.
    """
    old_private, old_public = rsa_pem()
    new_private, new_public = pem(ec.generate_private_key(ec.SECP256R1()))
    with freeze_time(START) as clock:
        old = jwt_auth(
            use_auth,
            SIGNING_KEY=old_private,
            VERIFYING_KEY=old_public,
            ALGORITHM="RS256",
        )
        token = old.encode(user(), uuid4())
        rotated = jwt_auth(
            use_auth,
            SIGNING_KEY=new_private,
            VERIFYING_KEY=new_public,
            ALGORITHM="ES256",
            PREVIOUS_KEYS=[
                {"KEY": old_public, "ALGORITHM": "RS256", "RETIRED_AT": START}
            ],
        )
        fresh = rotated.encode(user(), uuid4())
        decoded = [rotated.decode(token), rotated.decode(fresh)]
        clock.move_to(at(START, 5))
        refused = refusal(rotated, token)

    assert [claims["sub"] for claims in decoded] == [
        verified(token, old_public, "RS256")["sub"],
        verified(fresh, new_public, "ES256")["sub"],
    ]
    assert refused == "Invalid token."
    assert jwt.get_unverified_header(fresh) == {
        "alg": "ES256",
        "kid": rotated.key_id,
        "typ": "JWT",
    }


def test_asymmetric_key_ids_are_jwk_thumbprints(use_auth: UseAuth) -> None:
    """RFC 7638's own example key gets RFC 7638's own thumbprint."""
    loaded = RSAAlgorithm.from_jwk({"kty": "RSA", "n": RFC_7638_N, "e": "AQAB"})
    assert isinstance(loaded, rsa.RSAPublicKey)
    public = loaded.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    secret, current = rsa_pem()
    with freeze_time(START):
        authenticator = jwt_auth(
            use_auth,
            SIGNING_KEY=secret,
            VERIFYING_KEY=current,
            ALGORITHM="RS256",
            PREVIOUS_KEYS=[{"KEY": public, "RETIRED_AT": START}],
        )

    assert set(authenticator.keys) == {authenticator.key_id, RFC_7638_THUMBPRINT}


@pytest.mark.parametrize(
    ("algorithm", "key"),
    [
        ("ES384", lambda: ec.generate_private_key(ec.SECP384R1())),
        ("EdDSA", ed25519.Ed25519PrivateKey.generate),
    ],
)
def test_every_asymmetric_kind_has_an_id(
    use_auth: UseAuth,
    algorithm: str,
    key: Any,  # noqa: ANN401 - a key factory.
) -> None:
    """EC and OKP keys get thumbprints too; the private key never shows in them."""
    secret, public = pem(key())
    authenticator = jwt_auth(
        use_auth, SIGNING_KEY=secret, VERIFYING_KEY=public, ALGORITHM=algorithm
    )
    token = authenticator.encode(user(), uuid4())

    assert len(authenticator.key_id) == 43  # A whole SHA-256, base64url.
    assert kid(token) == authenticator.key_id
    assert authenticator.decode(token)["sub"]


# What the key set refuses --------------------------------------------------------


def test_unknown_or_malformed_key_ids_are_refused(use_auth: UseAuth) -> None:
    """A ``kid`` the key set lacks is refused, never tried against every key."""
    with freeze_time(START):
        authenticator = jwt_auth(
            use_auth,
            SIGNING_KEY=NEW,
            PREVIOUS_KEYS=[{"KEY": OLD, "RETIRED_AT": START}],
        )
        claims = verified(authenticator.encode(user(), uuid4()), NEW)

        def signed(key: str, header: dict[str, Any]) -> str:
            head = segment({"alg": "HS256", "typ": "JWT", **header})
            body = f"{head}.{segment(claims)}"
            mac = hmac.new(key.encode(), body.encode(), hashlib.sha256).digest()
            return f"{body}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"

        tokens = [
            signed(NEW, {"kid": "unknown"}),
            signed(OLD, {"kid": "unknown"}),
            signed(NEW, {"kid": 7}),
            signed(NEW, {"kid": ["a"]}),
            signed(NEW, {"kid": ""}),
            signed(NEW, {"kid": None}),
            signed(NEW, {"kid": authenticator.key_id + "x"}),
            "not.a.token",
            f"{encoded(b'[1]')}.{segment(claims)}.x",
            f"{encoded(b'[' * 100_000)}.{segment(claims)}.x",
            f"{encoded(b'{"kid": ')}.{segment(claims)}.x",
            "%%%.x.y",
        ]
        details = [refusal(authenticator, token) for token in tokens]
        accepted = authenticator.decode(signed(NEW, {"kid": authenticator.key_id}))

    assert details == ["Invalid token."] * len(tokens)
    assert accepted == claims  # The signer above makes tokens that hold.


def test_a_tampered_key_id_breaks_the_signature(use_auth: UseAuth) -> None:
    """The header is signed: moving a token to another key's id refuses it."""
    with freeze_time(START):
        old = jwt_auth(use_auth, SIGNING_KEY=OLD)
        token = old.encode(user(), uuid4())
        rotated = jwt_auth(
            use_auth,
            SIGNING_KEY=NEW,
            PREVIOUS_KEYS=[{"KEY": OLD, "RETIRED_AT": START}],
        )
        _, payload, signature = token.split(".")
        moved = segment({**jwt.get_unverified_header(token), "kid": rotated.key_id})
        tampered = f"{moved}.{payload}.{signature}"

        assert rotated.decode(token)["sub"]
        assert refusal(rotated, tampered) == "Invalid token."


def test_the_algorithm_is_the_keys_never_the_tokens(use_auth: UseAuth) -> None:
    """A token's ``alg`` never chooses: each key verifies with its own only.

    The classic confusion: the RSA public key used as an HMAC secret, under
    the RSA key's id. And ``none``, and another HMAC algorithm.
    """
    secret, public = rsa_pem()
    with freeze_time(START):
        authenticator = jwt_auth(
            use_auth,
            SIGNING_KEY=secret,
            VERIFYING_KEY=public,
            ALGORITHM="RS256",
            PREVIOUS_KEYS=[{"KEY": OLD, "ALGORITHM": "HS256", "RETIRED_AT": START}],
        )
        token = authenticator.encode(user(), uuid4())
        _, payload, _ = token.split(".")
        claims = verified(token, public, "RS256")
        current = authenticator.key_id
        previous = next(key for key in authenticator.keys if key != current)

        def forged(header: dict[str, Any], key: bytes = public.encode()) -> str:
            head = segment(header)
            signing = hmac.new(key, f"{head}.{payload}".encode(), hashlib.sha256)
            raw = base64.urlsafe_b64encode(signing.digest()).rstrip(b"=")
            return f"{head}.{payload}.{raw.decode()}"

        tokens = [
            forged({"alg": "HS256", "typ": "JWT", "kid": current}),
            forged({"alg": "HS256", "typ": "JWT"}),
            forged({"alg": "HS512", "typ": "JWT", "kid": previous}, OLD.encode()),
            f"{segment({'alg': 'none', 'kid': current})}.{payload}.",
            f"{segment({'alg': 'none', 'kid': previous})}.{payload}.",
            jwt.encode(claims, secret, algorithm="RS256", headers={"kid": previous}),
        ]
        details = [refusal(authenticator, token) for token in tokens]

    assert details == ["Invalid token."] * len(tokens)


def test_tokens_without_a_key_id_are_the_signing_keys_alone(use_auth: UseAuth) -> None:
    """0.10's tokens, without ``kid``, keep working with the key that signed them.

    They are checked against the signing key only, never a previous one: a
    token without ``kid`` signed by a retired key is refused, so the
    upgrade logs nobody out and a rotation still ends what the old key
    signed.
    """
    with freeze_time(START):
        claims = verified(
            jwt_auth(use_auth, SIGNING_KEY=OLD).encode(user(), uuid4()), OLD
        )
        legacy = jwt.encode(claims, OLD, algorithm="HS256")
        upgraded = jwt_auth(use_auth, SIGNING_KEY=OLD)
        rotated = jwt_auth(
            use_auth,
            SIGNING_KEY=NEW,
            PREVIOUS_KEYS=[{"KEY": OLD, "RETIRED_AT": START}],
        )

        assert kid(legacy) is None
        assert upgraded.decode(legacy)["sub"] == claims["sub"]
        assert refusal(rotated, legacy) == "Invalid token."


# The settings ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("previous", "problem"),
    [
        (
            [{"KEY": NEW, "RETIRED_AT": START}],
            (
                "auth.E008: AUTH.JWT.PREVIOUS_KEYS[0] is the signing key: a key "
                "that signs is not retired; give the old key, or none."
            ),
        ),
        (
            [
                {"KEY": OLD, "RETIRED_AT": START},
                {"KEY": OLD, "RETIRED_AT": at(START, -60)},
            ],
            (
                "auth.E008: AUTH.JWT.PREVIOUS_KEYS[1] is the key of "
                "AUTH.JWT.PREVIOUS_KEYS[0]; list each key once."
            ),
        ),
        (
            [{"KEY": OLD, "RETIRED_AT": at(START, 24 * 60 + 1)}],
            (
                "auth.E009: AUTH.JWT.PREVIOUS_KEYS[0].RETIRED_AT is "
                "2026-11-03 09:01:00+00:00, more than a day ahead: a previous key "
                "verifies only what it signed until it retired, plus "
                "ACCESS_TOKEN_TTL and LEEWAY. Set the time its rollout ends."
            ),
        ),
    ],
)
def test_unsafe_key_sets_are_refused(
    use_auth: UseAuth, previous: list[dict[str, Any]], problem: str
) -> None:
    """The signing key again, a key twice, or a retirement past the bound."""
    with freeze_time(START), pytest.raises(CheckFailed) as refused:
        jwt_auth(use_auth, SIGNING_KEY=NEW, PREVIOUS_KEYS=previous)

    assert str(refused.value) == f"AUTH settings cannot be used:\n  {problem}"


def test_a_retirement_a_day_ahead_is_within_the_bound(use_auth: UseAuth) -> None:
    """A rollout may end up to a day after the new key starts signing."""
    with freeze_time(START):
        authenticator = jwt_auth(
            use_auth,
            SIGNING_KEY=NEW,
            PREVIOUS_KEYS=[{"KEY": OLD, "RETIRED_AT": at(START, 24 * 60)}],
        )

    assert len(authenticator.keys) == 2


def test_the_same_asymmetric_key_is_found_whatever_its_pem(use_auth: UseAuth) -> None:
    """The public key's thumbprint, not its text, says it is the signing key."""
    secret, public = rsa_pem()
    rewrapped = public.replace("\n", "\r\n")
    with freeze_time(START), pytest.raises(CheckFailed, match=r"auth\.E008"):
        jwt_auth(
            use_auth,
            SIGNING_KEY=secret,
            VERIFYING_KEY=public,
            ALGORITHM="RS256",
            PREVIOUS_KEYS=[
                {"KEY": rewrapped, "ALGORITHM": "PS256", "RETIRED_AT": START}
            ],
        )


@pytest.mark.parametrize(
    ("previous", "message"),
    [
        (
            {"KEY": "o" * 31},
            "AUTH.JWT.PREVIOUS_KEYS[0].KEY must have at least 32 bytes for HS256.",
        ),
        (
            {"KEY": "o" * 47, "ALGORITHM": "HS384"},
            "AUTH.JWT.PREVIOUS_KEYS[0].KEY must have at least 48 bytes for HS384.",
        ),
        (
            {"KEY": "not a key", "ALGORITHM": "ES256"},
            "AUTH.JWT.PREVIOUS_KEYS[0].KEY is not a public key for ES256 (",
        ),
        (
            {
                "KEY": PEM_LIKE,
                "ALGORITHM": "HS256",
            },
            "AUTH.JWT.PREVIOUS_KEYS[0].KEY is not a secret for HS256 (",
        ),
    ],
)
def test_previous_keys_are_checked(
    use_auth: UseAuth, previous: dict[str, Any], message: str
) -> None:
    """Short secrets, a public key as a secret, and broken public keys stop startup.

    The exception's name follows, which differs between releases of PyJWT.
    """
    with freeze_time(START), pytest.raises(ImproperlyConfigured) as refused:
        jwt_auth(
            use_auth,
            SIGNING_KEY=NEW,
            PREVIOUS_KEYS=[{**previous, "RETIRED_AT": START}],
        )

    named = r"\w+Error\)\." if message.endswith("(") else ""  # The exception's.
    assert re.fullmatch(re.escape(message) + named, str(refused.value))
    assert previous["KEY"] not in str(refused.value)


def test_a_previous_private_key_is_refused(use_auth: UseAuth) -> None:
    """Verifying needs the public key only; the private one should be destroyed."""
    old_private, _ = rsa_pem()
    with freeze_time(START), pytest.raises(ImproperlyConfigured) as refused:
        jwt_auth(
            use_auth,
            SIGNING_KEY=NEW,
            PREVIOUS_KEYS=[
                {"KEY": old_private, "ALGORITHM": "RS256", "RETIRED_AT": START}
            ],
        )

    assert str(refused.value) == (
        "AUTH.JWT.PREVIOUS_KEYS[0].KEY is a private key: give its public key "
        "in PEM; a retired key only verifies."
    )
    assert old_private not in str(refused.value)


def test_previous_keys_are_bounded_and_dated() -> None:
    """At most three, each with an aware retirement time, hidden in reprs."""
    entry = {"KEY": OLD, "RETIRED_AT": START}

    with pytest.raises(ValidationError, match="at most 3 items"):
        JWTSettings(SIGNING_KEY=NEW, PREVIOUS_KEYS=[entry] * 4)
    with pytest.raises(ValidationError, match="timezone"):
        JWTSettings(PREVIOUS_KEYS=[{"KEY": OLD, "RETIRED_AT": "2026-11-02T09:00"}])
    with pytest.raises(ValidationError, match="RETIRED_AT"):
        JWTSettings(PREVIOUS_KEYS=[{"KEY": OLD}])
    with pytest.raises(ValidationError, match="Extra inputs"):
        JWTSettings(PREVIOUS_KEYS=[{**entry, "VERIFY_UNTIL": START}])
    config = JWTSettings(SIGNING_KEY=NEW, PREVIOUS_KEYS=[entry])
    assert OLD not in repr(config)
    assert config.PREVIOUS_KEYS[0].ALGORITHM is None


# Through the API ------------------------------------------------------------


JWT: dict[str, Any] = {
    "AUTHENTICATION_CLASSES": [
        f"{AUTHENTICATION}.JWTAuthentication",
        f"{AUTHENTICATION}.TokenAuthentication",
    ],
}


def test_a_rotation_keeps_logged_in_clients_working(api: Api) -> None:
    """Access tokens of the old key work after the rollout, then refresh to the new.

    The refresh token is opaque and stored: a rotation never touches it.
    """
    from domainkit_tests.test_auth_http import bearer, create_user, login

    create_user("alice")
    with freeze_time(START, tick=True) as clock:
        before = api(**JWT, JWT={"SIGNING_KEY": OLD})
        issued = login(before, "alice")
        after = api(
            **JWT,
            JWT={
                "SIGNING_KEY": NEW,
                "PREVIOUS_KEYS": [{"KEY": OLD, "RETIRED_AT": START}],
            },
        )
        old_access = bearer(issued["access_token"])
        served = after.get("/private", headers=old_access).status_code
        clock.move_to(at(START, 5))
        expired = after.get("/whoami", headers=old_access).json()["detail"]
        refreshed = after.post(
            "/auth/refresh", json={"refresh_token": issued["refresh_token"]}
        ).json()
        new_access = refreshed["access_token"]
        served_again = after.get("/private", headers=bearer(new_access)).status_code

    assert served == 200
    assert expired == "Invalid token."
    assert kid(new_access) != kid(issued["access_token"])
    assert served_again == 200


def test_key_ids_are_checked_before_anything_else(use_auth: UseAuth) -> None:
    """``authenticate`` refuses an unknown key id as a bad token: 401."""
    with freeze_time(START):
        authenticator = jwt_auth(use_auth, SIGNING_KEY=NEW)
        claims = verified(authenticator.encode(user(), uuid4()), NEW)
        token = jwt.encode(claims, NEW, algorithm="HS256", headers={"kid": "x"})
        with pytest.raises(AuthenticationFailed) as refused:
            asyncio.run(authenticator.authenticate(connection(token)))

    assert (refused.value.status_code, refused.value.detail) == (
        401,
        "Invalid token.",
    )
