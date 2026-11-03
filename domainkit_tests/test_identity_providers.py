"""Logging in and linking through OpenID Connect and OAuth 2 providers."""

import base64
import hashlib
import json
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient

from domainkit.conf import override_settings
from domainkit.contrib.identity import providers as providers_module
from domainkit.http.client import stub
from domainkit_tests.conftest import (
    IDENTITY,
    IDENTITY_DOMAINS,
    ExitStackClients,
    Migrated,
    Shape,
)
from domainkit_tests.test_auth_http import PASSWORD, bearer, create_user, login

PROVIDERS = Shape(
    "providers",
    "providers",
    {
        **IDENTITY.settings,
        "INSTALLED_DOMAINS": [*IDENTITY_DOMAINS, "domainkit.contrib.identity"],
        "ROOT_ROUTER": "domainkit_tests.sample_provider_routes.router",
    },
)
type App = Callable[..., TestClient]
ISSUER = "https://idp.example"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OIDC = {
    "ISSUER": ISSUER,
    "CLIENT_ID": "billing",
    "CLIENT_SECRET": "s3cret",
    "REDIRECT_URI": "https://app.example.com/login/idp",
}


class FakeIdP:
    """An OpenID Connect provider: discovery, keys, and a token endpoint with PKCE."""

    def __init__(self) -> None:
        """No login started yet."""
        self.pending: dict[str, dict[str, str]] = {}
        self.subject = "user-1"
        self.email: str | None = "ann@example.com"
        self.verified = True
        self.claims: dict[str, Any] = {}
        self.kid = "k1"
        self.forms: list[dict[str, str]] = []

    def authorize(self, url: str) -> tuple[str, str]:
        """The user logs in at the authorization URL: the code and state sent back."""
        query = dict(parse_qsl(urlsplit(url).query))
        assert query["code_challenge_method"] == "S256"
        assert query["redirect_uri"] == OIDC["REDIRECT_URI"]
        code = f"code-{len(self.pending)}"
        self.pending[code] = query
        return code, query["state"]

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer discovery, keys, and token requests."""
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if path == "/jwks":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
            return httpx2.Response(
                200, json={"keys": [{**jwk, "kid": "k1", "alg": "RS256"}]}
            )
        assert path == "/token"
        form = dict(parse_qsl(request.content.decode()))
        self.forms.append(form)
        started = self.pending.pop(form["code"], None)
        if started is None:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if challenge != started["code_challenge"]:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": "billing",
            "sub": self.subject,
            "iat": now,
            "exp": now + 300,
            "nonce": started["nonce"],
            **(
                {"email": self.email, "email_verified": self.verified}
                if self.email
                else {}
            ),
            **self.claims,
        }
        token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": self.kid})
        return httpx2.Response(200, json={"access_token": "at", "id_token": token})


@pytest.fixture
def idp() -> Iterator[FakeIdP]:
    """The provider, answering every call made in the test."""
    fake = FakeIdP()
    providers_module.forget()
    with stub(fake):
        yield fake
    providers_module.forget()


@pytest.fixture
def app(migrated: Migrated) -> Iterator[App]:
    """``app(**IDENTITY)``: a client of the project with these providers."""
    with migrated(PROVIDERS), ExitStackClients() as clients:

        def make(**identity: Any) -> TestClient:  # noqa: ANN401 - IDENTITY keys.
            clients.enter(override_settings(IDENTITY=identity))
            return clients.client(("testclient", 50000))

        yield make


def log_in_at(client: TestClient, idp: FakeIdP, name: str = "idp") -> Any:  # noqa: ANN401
    """Start a login, log in at the provider, and come back: the answer."""
    started = client.post(f"/auth/identity/{name}/login")
    assert started.status_code == 200, started.json()
    code, state = idp.authorize(started.json()["authorization_url"])
    return client.post(
        f"/auth/identity/{name}/callback", json={"code": code, "state": state}
    )


def test_a_new_identity_signs_up_and_logs_in_again(app: App, idp: FakeIdP) -> None:
    """SIGNUP on, a verified email: an account is made, then found by the subject."""
    client = app(PROVIDERS={"idp": OIDC}, SIGNUP=True)

    first = log_in_at(client, idp)
    again = log_in_at(client, idp)

    assert first.status_code == 200, first.json()
    assert first.headers["cache-control"] == "no-store"
    me = client.get("/auth/me", headers=bearer(first.json()["access_token"]))
    assert (me.json()["username"], me.json()["email"]) == (
        "ann@example.com",
        "ann@example.com",
    )
    assert me.json()["email_verified"] is True
    assert again.status_code == 200
    linked = client.get(
        "/auth/identity/linked", headers=bearer(again.json()["access_token"])
    )
    assert [(each["provider"], each["email"]) for each in linked.json()] == [
        ("idp", "ann@example.com")
    ]
    assert idp.forms[0]["client_secret"] == "s3cret"


def test_an_account_with_the_email_is_never_handed_over(app: App, idp: FakeIdP) -> None:
    """409: whoever controls the provider's account must not get the local one."""
    client = app(PROVIDERS={"idp": OIDC}, SIGNUP=True)
    create_user("ann", email="ann@example.com")

    answer = log_in_at(client, idp)

    assert (answer.status_code, answer.json()["code"]) == (409, "identity_email_taken")


def test_without_signup_or_a_verified_email_nothing_is_made(
    app: App, idp: FakeIdP
) -> None:
    """422 identity_not_linked, whatever the provider says."""
    off = app(PROVIDERS={"idp": OIDC})
    refused = log_in_at(off, idp)
    on = app(PROVIDERS={"idp": OIDC}, SIGNUP=True)
    idp.verified = False
    unverified = log_in_at(on, idp)

    assert (refused.status_code, refused.json()["code"]) == (422, "identity_not_linked")
    assert unverified.json()["code"] == "identity_not_linked"


def test_a_user_links_an_identity_then_logs_in_with_it(app: App, idp: FakeIdP) -> None:
    """Linking needs a login; the identity then logs the user in, and unlinks."""
    client = app(PROVIDERS={"idp": OIDC})
    create_user("bob")
    headers = bearer(login(client, "bob")["access_token"])

    started = client.post("/auth/identity/idp/link", headers=headers)
    code, state = idp.authorize(started.json()["authorization_url"])
    linked = client.post(
        "/auth/identity/idp/callback", json={"code": code, "state": state}
    )
    logged_in = log_in_at(client, idp)
    me = client.get("/auth/me", headers=bearer(logged_in.json()["access_token"]))
    removed = client.delete("/auth/identity/linked/idp", headers=headers)
    after = log_in_at(client, idp)

    assert linked.json() == {"linked": "idp"}
    assert me.json()["username"] == "bob"
    assert removed.status_code == 204
    assert after.json()["code"] == "identity_not_linked"
    assert client.post("/auth/identity/idp/link").status_code == 401


def test_an_identity_linked_elsewhere_and_the_last_way_in(
    app: App, idp: FakeIdP
) -> None:
    """Another user's identity is refused; an account's only way in stays."""
    client = app(PROVIDERS={"idp": OIDC}, SIGNUP=True)
    made = log_in_at(client, idp).json()["access_token"]  # Its password is unusable.
    create_user("bob")
    bob = bearer(login(client, "bob")["access_token"])

    started = client.post("/auth/identity/idp/link", headers=bob)
    code, state = idp.authorize(started.json()["authorization_url"])
    taken = client.post(
        "/auth/identity/idp/callback", json={"code": code, "state": state}
    )
    last = client.delete("/auth/identity/linked/idp", headers=bearer(made))

    assert (taken.status_code, taken.json()["code"]) == (409, "identity_taken")
    assert (last.status_code, last.json()["code"]) == (422, "identity_last_way_in")


def test_a_state_works_once_and_only_at_its_provider(app: App, idp: FakeIdP) -> None:
    """A replayed, unknown, or other provider's state is refused before any exchange."""
    client = app(PROVIDERS={"idp": OIDC, "other": OIDC}, SIGNUP=True)
    started = client.post("/auth/identity/idp/login")
    code, state = idp.authorize(started.json()["authorization_url"])

    elsewhere = client.post(
        "/auth/identity/other/callback", json={"code": code, "state": state}
    )
    replayed = client.post(
        "/auth/identity/idp/callback", json={"code": code, "state": state}
    )
    unknown = client.post("/auth/identity/nope/login")

    assert elsewhere.json()["code"] == "identity_not_linked"
    assert replayed.json()["code"] == "identity_not_linked"
    assert idp.forms == []  # No code was ever sent with a state that failed.
    assert (unknown.status_code, unknown.json()["code"]) == (
        404,
        "unknown_identity_provider",
    )
    assert client.get("/auth/identity/providers").json() == {
        "providers": ["idp", "other"],
        "saml": [],
    }


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "someone-else"},
        {"iss": "https://evil.example"},
        {"exp": 1},
        {"nonce": "another-login"},
    ],
)
def test_id_tokens_that_do_not_hold_are_refused(
    app: App, idp: FakeIdP, claims: dict[str, Any]
) -> None:
    """Audience, issuer, expiry, nonce: 502 identity_provider_error, no more said."""
    client = app(PROVIDERS={"idp": OIDC}, SIGNUP=True)
    idp.claims = claims

    answer = log_in_at(client, idp)

    assert (answer.status_code, answer.json()["code"]) == (
        502,
        "identity_provider_error",
    )


def test_a_token_signed_with_a_key_not_published_is_refused(
    app: App, idp: FakeIdP
) -> None:
    """An unknown key id reads the keys again, once; still unknown, refused."""
    client = app(PROVIDERS={"idp": OIDC}, SIGNUP=True)
    idp.kid = "rotated-away"

    assert log_in_at(client, idp).json()["code"] == "identity_provider_error"


def test_a_user_with_a_second_factor_gets_its_challenge(app: App, idp: FakeIdP) -> None:
    """202 and a challenge, as a password login answers."""
    from domainkit.contrib.auth import AuthUnitOfWork
    from domainkit.management.base import run_async

    client = app(PROVIDERS={"idp": OIDC}, SIGNUP=True)
    made = log_in_at(client, idp).json()["access_token"]
    me = client.get("/auth/me", headers=bearer(made)).json()

    async def enable() -> None:
        from uuid import UUID

        async with AuthUnitOfWork() as uow:
            device = await uow.mfa.start(UUID(me["id"]), b"0" * 20)
            await uow.mfa.confirm(device, step=1)
            await uow.commit()

    run_async(enable())
    answer = log_in_at(client, idp)

    assert answer.status_code == 202
    assert answer.json()["mfa_required"] is True


def test_github_speaks_oauth_2(app: App) -> None:
    """The token, the user, and the verified primary email; PKCE too."""
    seen: list[httpx2.Request] = []

    def github(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path == "/login/oauth/access_token":
            return httpx2.Response(200, json={"access_token": "gho_x"})
        if request.url.path == "/user":
            return httpx2.Response(200, json={"id": 42, "login": "octo"})
        return httpx2.Response(
            200,
            json=[
                {"email": "other@example.com", "primary": False, "verified": True},
                {"email": "octo@example.com", "primary": True, "verified": True},
            ],
        )

    config = {
        "KIND": "github",
        "CLIENT_ID": "gh",
        "CLIENT_SECRET": "x",
        "REDIRECT_URI": "https://a/cb",
    }
    client = app(PROVIDERS={"github": config}, SIGNUP=True)
    with stub(github):
        started = client.post("/auth/identity/github/login").json()["authorization_url"]
        state = dict(parse_qsl(urlsplit(started).query))["state"]
        answer = client.post(
            "/auth/identity/github/callback", json={"code": "c", "state": state}
        )

    assert started.startswith("https://github.com/login/oauth/authorize?")
    assert answer.status_code == 200, answer.json()
    assert seen[1].headers["authorization"] == "Bearer gho_x"


def test_presets_name_their_issuers_and_apple_signs_its_secret() -> None:
    """Google's, a Microsoft tenant's, Apple's; Apple's secret is an ES256 JWT."""
    from domainkit.contrib.identity.providers import make_provider
    from domainkit.contrib.identity.settings import ProviderSettings
    from domainkit.exceptions import ImproperlyConfigured

    base = {"CLIENT_ID": "c", "REDIRECT_URI": "https://a/cb"}
    apple_key = ec.generate_private_key(ec.SECP256R1())
    from cryptography.hazmat.primitives import serialization

    pem = apple_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    google = make_provider("g", ProviderSettings(KIND="google", **base))
    microsoft = make_provider(
        "m", ProviderSettings(KIND="microsoft", TENANT="t-1", **base)
    )
    apple = make_provider(
        "a",
        ProviderSettings(
            KIND="apple",
            APPLE_TEAM_ID="TEAM",
            APPLE_KEY_ID="KEY",
            APPLE_PRIVATE_KEY=pem,
            **base,
        ),
    )

    assert google.issuer() == "https://accounts.google.com"  # type: ignore[attr-defined]
    assert microsoft.issuer() == "https://login.microsoftonline.com/t-1/v2.0"  # type: ignore[attr-defined]
    secret = apple.client_secret()
    assert secret is not None
    claims = jwt.decode(
        secret,
        apple_key.public_key(),
        algorithms=["ES256"],
        audience="https://appleid.apple.com",
    )
    assert (claims["iss"], claims["sub"]) == ("TEAM", "c")
    assert jwt.get_unverified_header(secret)["kid"] == "KEY"
    with pytest.raises(ImproperlyConfigured, match="TENANT"):
        make_provider("m", ProviderSettings(KIND="microsoft", **base)).issuer()  # type: ignore[attr-defined]
    with pytest.raises(ImproperlyConfigured, match="APPLE_TEAM_ID"):
        make_provider("a", ProviderSettings(KIND="apple", **base)).client_secret()
    with pytest.raises(ImproperlyConfigured, match="ISSUER"):
        make_provider("o", ProviderSettings(**base)).issuer()  # type: ignore[attr-defined]
    assert PASSWORD
