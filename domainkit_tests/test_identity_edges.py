"""The refusals and edges of identity providers: each one says no, and why."""

import base64
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx2
import jwt
import pytest
from lxml import etree

from domainkit.apps import registry
from domainkit.contrib.auth import AuthUnitOfWork, User
from domainkit.contrib.auth.events import UserDeleted
from domainkit.contrib.identity import ProviderError, providers
from domainkit.contrib.identity import ldap as ldap_module
from domainkit.contrib.identity.apps import forget_identities
from domainkit.contrib.identity.providers import Claims, make_provider
from domainkit.contrib.identity.saml import AssertionRefused, verified_claims
from domainkit.contrib.identity.services import account_for, link
from domainkit.contrib.identity.settings import (
    LdapSettings,
    ProviderSettings,
    SamlSettings,
)
from domainkit.contrib.identity.stores import Flow, IdentityUnitOfWork
from domainkit.db.routing import router
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.client import stub
from domainkit.management.base import run_async
from domainkit_tests.test_auth_http import create_user
from domainkit_tests.test_identity_ldap import LDAP, App
from domainkit_tests.test_identity_providers import ISSUER, KEY, OIDC, app  # noqa: F401
from domainkit_tests.test_identity_saml import (
    CERT,
    OKTA,
    SIGNER,
    assertion_xml,
)
from domainkit_tests.test_identity_saml import (
    KEY as SAML_KEY,
)

SIGNED_UP = {"SIGNUP": True}


def oidc(**changes: Any) -> Any:  # noqa: ANN401 - a provider.
    """The test provider, its settings changed."""
    return make_provider("idp", ProviderSettings(**{**OIDC, **changes}))


def discovery(**changes: Any) -> dict[str, Any]:  # noqa: ANN401 - JSON.
    """The provider's discovery document."""
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        **changes,
    }


def jwks(*extra: dict[str, Any]) -> dict[str, Any]:
    """The provider's key set, with ``extra`` keys first."""
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    return {"keys": [*extra, {**jwk, "kid": "k1"}]}


def provider_answering(**answers: httpx2.Response) -> Any:  # noqa: ANN401 - a handler.
    """A provider whose discovery, keys, and token endpoint answer as given."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.strip("/").replace("/", "_").replace(".", "")
        key = {"well-known_openid-configuration": "discovery"}.get(path, path)
        if key in answers:
            return answers[key]
        if key == "discovery":
            return httpx2.Response(200, json=discovery())
        return httpx2.Response(200, json=jwks())

    return answer


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    """No document or key kept between tests."""
    providers.forget()
    yield
    providers.forget()


@pytest.mark.anyio
async def test_a_provider_that_cannot_answer_is_a_provider_error() -> None:
    """Unreachable, a 500, another issuer, not JSON, a list, refused codes."""

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route")

    cases: list[tuple[Any, str]] = [
        (down, "could not be reached"),
        (provider_answering(discovery=httpx2.Response(500)), "unavailable"),
        (
            provider_answering(
                discovery=httpx2.Response(200, json=discovery(issuer="x"))
            ),
            "another issuer",
        ),
        (
            provider_answering(discovery=httpx2.Response(200, content=b"<html>")),
            "not JSON",
        ),
        (
            provider_answering(discovery=httpx2.Response(200, json=[1])),
            "not a JSON object",
        ),
    ]
    for handler, message in cases:
        providers.forget()
        with stub(handler), pytest.raises(ProviderError, match=message):
            await oidc().authorization_url("s", "n", "v")
    with (
        stub(provider_answering(token=httpx2.Response(400))),
        pytest.raises(ProviderError, match="refused the code"),
    ):
        await oidc(CLIENT_SECRET=None).exchange("c", "v", "n")
    with (
        stub(provider_answering(token=httpx2.Response(200, json={}))),
        pytest.raises(ProviderError, match="without an ID token"),
    ):
        await oidc().exchange("c", "v", "n")


def signed(claims: dict[str, Any], **header: Any) -> str:  # noqa: ANN401
    """An ID token signed by the provider's key."""
    now = int(time.time())
    base = {"iss": ISSUER, "aud": "billing", "sub": "s", "iat": now, "exp": now + 60}
    return jwt.encode(
        {**base, **claims}, KEY, algorithm="RS256", headers={"kid": "k1", **header}
    )


@pytest.mark.anyio
async def test_id_tokens_that_are_odd_are_refused() -> None:
    """Malformed, HMAC-signed, a key that will not load, keys gone, none at all."""
    provider = oidc()
    hmac = jwt.encode(
        {"sub": "s"}, "secret-of-32-bytes-or-more-please!", algorithm="HS256"
    )
    with stub(provider_answering()):
        with pytest.raises(ProviderError, match="malformed"):
            await provider.verify("not.a.token", "n")
        with pytest.raises(ProviderError, match="HS256"):
            await provider.verify(hmac, "n")
    providers.forget()
    with stub(
        provider_answering(
            jwks=httpx2.Response(200, json=jwks({"kty": "nope", "kid": "k1"}))
        )
    ):
        assert (await provider.verify(signed({"nonce": "n"}), "n"))["sub"] == "s"
    providers.forget()
    with (
        stub(provider_answering(jwks=httpx2.Response(503))),
        pytest.raises(ProviderError, match="keys are unavailable"),
    ):
        await provider.verify(signed({}), "n")
    providers.forget()
    with (
        stub(provider_answering(jwks=httpx2.Response(200, json={}))),
        pytest.raises(ProviderError, match="published no keys"),
    ):
        await provider.verify(signed({}), "n")


@pytest.mark.anyio
async def test_github_refusals() -> None:
    """No token, or no user: the login fails."""
    github = make_provider(
        "gh",
        ProviderSettings(KIND="github", CLIENT_ID="c", REDIRECT_URI="https://a/cb"),
    )

    def no_token(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"error": "bad_verification_code"})

    def no_user(request: httpx2.Request) -> httpx2.Response:
        if "access_token" in request.url.path:
            return httpx2.Response(200, json={"access_token": "t"})
        return httpx2.Response(401)

    with stub(no_token), pytest.raises(ProviderError, match="without a token"):
        await github.exchange("c", "v", "n")
    with stub(no_user), pytest.raises(ProviderError, match="would not say"):
        await github.exchange("c", "v", "n")


def saml(document: str) -> str:
    """``document`` signed by the provider, in base64."""
    return base64.b64encode(
        etree.tostring(SIGNER.sign(etree.fromstring(document), key=SAML_KEY, cert=CERT))
    ).decode()


def test_saml_documents_without_what_a_login_needs_are_refused() -> None:
    """Another element signed; a response without an assertion; parts missing."""
    config = SamlSettings(**OKTA)
    now = datetime.now(UTC)
    protocol = 'xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"'
    success = (
        "<samlp:Status><samlp:StatusCode "
        'Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
    )
    stripped = etree.fromstring(assertion_xml("_r"))
    no_conditions = etree.fromstring(assertion_xml("_r"))
    no_conditions.remove(no_conditions.find("{*}Conditions"))
    no_subject = etree.fromstring(assertion_xml("_r"))
    no_subject.remove(no_subject.find("{*}Subject"))
    cases = {
        "no response or assertion": saml('<other ID="_x"/>'),
        "no assertion": saml(
            f'<samlp:Response {protocol} ID="_r1">{success}</samlp:Response>'
        ),
        "no conditions": saml(etree.tostring(no_conditions).decode()),
        "no subject": saml(etree.tostring(no_subject).decode()),
    }
    for reason, encoded in cases.items():
        with pytest.raises(AssertionRefused, match=reason):
            verified_claims(config, encoded, "_r", now)
    assert stripped is not None


def test_a_saml_email_comes_from_the_name_id_when_no_attribute_is_named() -> None:
    """An emailAddress NameID is the email; another attribute is passed over."""
    config = SamlSettings(**{**OKTA, "EMAIL_ATTRIBUTE": None})
    document = assertion_xml("_r", subject="ann@example.com").replace(
        "<saml:NameID>",
        '<saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">',
    )
    other = SamlSettings(**{**OKTA, "EMAIL_ATTRIBUTE": "mail"})

    claims = verified_claims(config, saml(document), "_r", datetime.now(UTC))
    without = verified_claims(other, saml(assertion_xml("_r")), "_r", datetime.now(UTC))

    assert (claims.email, claims.email_verified) == ("ann@example.com", True)
    assert (without.email, without.email_verified) == (None, False)


def user_id(username: str) -> UUID:
    """The id of ``username``."""

    async def read() -> UUID:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            assert user is not None
            return user.id

    return run_async(read())


def test_accounts_inactive_gone_or_unmakeable(
    app: App,  # noqa: F811 - the fixture.
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inactive: refused; deleted: its link dropped; required fields: refused."""
    from domainkit.contrib.identity.exceptions import NoAccount

    app(**SIGNED_UP)
    claims = Claims("sub-1", "ann@example.com", email_verified=True)
    made = run_async(account_for("idp", claims))
    run_async(link(made.id, "idp", claims))  # Linked already: nothing to do.
    create_user("off", is_active=False)
    run_async(link(user_id("off"), "idp", Claims("sub-2")))
    with pytest.raises(NoAccount, match="cannot log in"):
        run_async(account_for("idp", Claims("sub-2")))

    async def forget_user() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("ann@example.com")
            assert user is not None
            await uow.users.remove(user)
            await uow.commit()

    run_async(forget_user())  # The link stays behind, its user gone.
    again = run_async(
        account_for("idp", Claims("sub-1", "ann@example.com", email_verified=True))
    )
    assert again.get_username() == "ann@example.com"
    taken = run_async(
        account_for("idp", Claims("sub-3", "ann+x@example.com", email_verified=True))
    )
    assert taken.get_username() == "ann+x@example.com"
    create_user("bo@example.com", email="bo@other.example")
    named = run_async(
        account_for("idp", Claims("sub-4", "bo@example.com", email_verified=True))
    )
    assert named.get_username() == "id-sub-4"
    monkeypatch.setattr(User, "REQUIRED_FIELDS", ("email", "first_name"))
    with pytest.raises(NoAccount, match="fields"):
        run_async(
            account_for("idp", Claims("sub-5", "cy@example.com", email_verified=True))
        )


def test_a_deleted_user_loses_their_identities(app: App) -> None:  # noqa: F811
    """UserDeleted unlinks every identity the user had."""
    app()
    create_user("ann")
    ann = user_id("ann")
    run_async(link(ann, "idp", Claims("sub-1")))

    run_async(forget_identities(UserDeleted(user_id=ann, username="ann")))

    async def linked() -> list[Any]:
        async with IdentityUnitOfWork() as uow:
            return await uow.identities.of_user(ann)

    assert run_async(linked()) == []


def test_identity_must_share_auths_database(
    app: App,  # noqa: F811 - the fixture.
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routed elsewhere, the app refuses to start."""
    app()
    config = registry.get_domain_config("identity")
    monkeypatch.setattr(router, "target_for", lambda label: f"database-of-{label}")

    with pytest.raises(ImproperlyConfigured, match="routed with 'auth'"):
        config.ready()


def test_a_flow_shows_no_secret() -> None:
    """Its repr names the provider and purpose only."""
    flow = Flow("state", "idp", "login", None, "verifier", "nonce", datetime.now(UTC))

    assert repr(flow) == "Flow(provider='idp', purpose='login')"


def test_the_ldap_connection_binds_the_reader_and_starts_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader binds at once, a user waits; START_TLS opens and upgrades."""
    calls: list[str] = []

    class Fake:
        def __init__(self, server: Any, **options: Any) -> None:  # noqa: ANN401
            self.user = options["user"]
            assert options["read_only"] is True

        def open(self) -> None:
            calls.append("open")

        def start_tls(self) -> None:
            calls.append("start_tls")

        def bind(self) -> bool:
            calls.append(f"bind {self.user}")
            return True

    monkeypatch.setattr("ldap3.Connection", Fake)
    config = LdapSettings(**{**LDAP, "START_TLS": True})

    ldap_module.connect(config, config.BIND_DN, "pw")
    ldap_module.connect(config, "uid=ann", "pw")

    assert calls == ["open", "start_tls", f"bind {config.BIND_DN}", "open", "start_tls"]
    calls.clear()
    ldap_module.connect(LdapSettings(**LDAP), "uid=ann", "pw")
    assert calls == []
