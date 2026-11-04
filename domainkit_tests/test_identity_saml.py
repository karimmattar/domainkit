"""SAML 2.0 single sign-on: only what the provider signed, for this request."""

import base64
import zlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from lxml import etree
from signxml.signer import XMLSigner

from domainkit.conf import override_settings
from domainkit_tests.conftest import ExitStackClients, Migrated
from domainkit_tests.test_auth_http import bearer, create_user
from domainkit_tests.test_identity_providers import PROVIDERS

type App = Callable[..., TestClient]

FRONTEND = "https://app.example.com/login/done"
SIGNER = XMLSigner(c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#")
"""Exclusive canonicalization, as identity providers sign SAML."""
ACS = "https://api.example.com/auth/identity/saml/okta/acs"


def certificate() -> tuple[str, str]:
    """A self-signed certificate and its key, in PEM."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.example")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode(), pem.decode()


CERT, KEY = certificate()
OTHER_CERT, OTHER_KEY = certificate()
OKTA = {
    "ENTITY_ID": "https://api.example.com/saml",
    "ACS_URL": ACS,
    "IDP_ENTITY_ID": "http://www.okta.com/exk1",
    "IDP_SSO_URL": "https://acme.okta.com/sso/saml",
    "IDP_CERTIFICATE": CERT,
    "EMAIL_ATTRIBUTE": "email",
}


def instant(delta: timedelta = timedelta()) -> str:
    """Now (or ``delta`` from now), as SAML writes it."""
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def assertion_xml(request_id: str, **overrides: str) -> str:
    """An assertion answering ``request_id``; ``overrides`` change its parts."""
    values = {
        "issuer": OKTA["IDP_ENTITY_ID"],
        "audience": OKTA["ENTITY_ID"],
        "recipient": ACS,
        "in_response_to": request_id,
        "not_before": instant(timedelta(minutes=-1)),
        "not_after": instant(timedelta(minutes=5)),
        "subject": "00u1",
        "email": "ann@example.com",
        **overrides,
    }
    return (
        '<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="_a1" Version="2.0" IssueInstant="{instant()}">'
        f"<saml:Issuer>{values['issuer']}</saml:Issuer>"
        f"<saml:Subject><saml:NameID>{values['subject']}</saml:NameID>"
        '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData InResponseTo="{values["in_response_to"]}" '
        f'Recipient="{values["recipient"]}" NotOnOrAfter="{values["not_after"]}"/>'
        "</saml:SubjectConfirmation></saml:Subject>"
        f'<saml:Conditions NotBefore="{values["not_before"]}" '
        f'NotOnOrAfter="{values["not_after"]}">'
        f"<saml:AudienceRestriction><saml:Audience>{values['audience']}</saml:Audience>"
        "</saml:AudienceRestriction></saml:Conditions>"
        '<saml:AttributeStatement><saml:Attribute Name="email">'
        f"<saml:AttributeValue>{values['email']}</saml:AttributeValue>"
        "</saml:Attribute></saml:AttributeStatement>"
        "</saml:Assertion>"
    )


def response(
    request_id: str,
    *,
    sign: str = "response",
    key: str = KEY,
    cert: str = CERT,
    status: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
    **overrides: str,
) -> str:
    """A SAMLResponse, base64, signed as ``sign`` says.

    ``sign`` is ``response``, ``assertion`` or ``none``.
    """
    assertion = assertion_xml(request_id, **overrides)
    if sign == "assertion":
        signed = SIGNER.sign(
            etree.fromstring(assertion), key=key, cert=cert, reference_uri="_a1"
        )
        assertion = etree.tostring(signed).decode()
    document = (
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        f'ID="_r1" Version="2.0" IssueInstant="{instant()}" '
        f'InResponseTo="{request_id}">'
        f'<samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>'
        f"{assertion}</samlp:Response>"
    )
    data = document.encode()
    if sign == "response":
        data = etree.tostring(SIGNER.sign(etree.fromstring(data), key=key, cert=cert))
    return base64.b64encode(data).decode()


@pytest.fixture
def app(migrated: Migrated) -> Iterator[App]:
    """``app(**IDENTITY)``: a client that does not follow redirects."""
    with migrated(PROVIDERS), ExitStackClients() as clients:

        def make(**identity: Any) -> TestClient:  # noqa: ANN401 - IDENTITY keys.
            clients.enter(override_settings(IDENTITY=identity))
            client = clients.client(("testclient", 50000))
            client.follow_redirects = False
            # Over HTTPS: the flow's cookie is Secure, as on any real site.
            client.base_url = client.base_url.copy_with(scheme="https")
            return client

        yield make


def start(client: TestClient) -> tuple[str, str]:
    """Start a SAML login: the AuthnRequest's id and the RelayState."""
    redirect = client.get("/auth/identity/saml/okta/login")
    assert redirect.status_code == 302
    location = redirect.headers["location"]
    assert location.startswith(OKTA["IDP_SSO_URL"] + "?")
    query = dict(parse_qsl(urlsplit(location).query))
    request = zlib.decompress(base64.b64decode(query["SAMLRequest"]), wbits=-15)
    root = etree.fromstring(request)
    assert root.get("AssertionConsumerServiceURL") == ACS
    return root.get("ID"), query["RelayState"]


def acs(client: TestClient, encoded: str, relay_state: str) -> dict[str, str]:
    """Post a response to the ACS: the query of the page the browser is sent to."""
    answer = client.post(
        "/auth/identity/saml/okta/acs",
        data={"SAMLResponse": encoded, "RelayState": relay_state},
    )
    assert answer.status_code == 303, answer.text
    location = answer.headers["location"]
    assert location.startswith(FRONTEND + "?")
    return dict(parse_qsl(urlsplit(location).query))


@pytest.mark.parametrize("sign", ["response", "assertion"])
def test_a_signed_response_logs_in_through_a_ticket(app: App, sign: str) -> None:
    """The page gets a ticket, trades it once for tokens; the account is made."""
    client = app(SAML={"okta": OKTA}, SIGNUP=True, FRONTEND_URL=FRONTEND)
    request_id, relay = start(client)

    sent = acs(client, response(request_id, sign=sign), relay)
    tokens = client.post("/auth/identity/ticket", json={"ticket": sent["ticket"]})
    again = client.post("/auth/identity/ticket", json={"ticket": sent["ticket"]})

    assert tokens.status_code == 200, tokens.json()
    me = client.get("/auth/me", headers=bearer(tokens.json()["access_token"])).json()
    assert me["email"] == "ann@example.com"
    assert again.json()["code"] == "identity_not_linked"


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("issuer", "http://evil.example"),
        ("audience", "https://other.example"),
        ("recipient", "https://other.example/acs"),
        ("in_response_to", "_another-request"),
        ("not_after", "2000-01-01T00:00:00Z"),
        ("not_before", "2999-01-01T00:00:00Z"),
        ("not_after", "not a time"),
    ],
)
def test_an_assertion_for_another_request_place_or_time_is_refused(
    app: App, change: str, value: str
) -> None:
    """Issuer, audience, recipient, request, and times: ``error=saml_refused``."""
    client = app(SAML={"okta": OKTA}, SIGNUP=True, FRONTEND_URL=FRONTEND)
    request_id, relay = start(client)

    assert acs(client, response(request_id, **{change: value}), relay) == {
        "error": "saml_refused"
    }


def test_unsigned_or_wrongly_signed_responses_are_refused(app: App) -> None:
    """Another key, no signature, a failed status, garbage, a used RelayState."""
    client = app(SAML={"okta": OKTA}, SIGNUP=True, FRONTEND_URL=FRONTEND)
    refused = {"error": "saml_refused"}
    encodings: tuple[Callable[[str], str], ...] = (
        lambda rid: response(rid, key=OTHER_KEY, cert=OTHER_CERT),
        lambda rid: response(rid, sign="none"),
        lambda rid: response(
            rid, status="urn:oasis:names:tc:SAML:2.0:status:Requester"
        ),
        lambda rid: "not base64!",
    )
    for encoded in encodings:
        request_id, relay = start(client)
        assert acs(client, encoded(request_id), relay) == refused
    request_id, relay = start(client)
    assert "ticket" in acs(client, response(request_id), relay)
    assert acs(client, response(request_id), relay) == refused  # Used once.


def test_refusals_of_the_account_reach_the_page(app: App) -> None:
    """No signup: identity_not_linked; the email taken: identity_email_taken."""
    off = app(SAML={"okta": OKTA}, FRONTEND_URL=FRONTEND)
    request_id, relay = start(off)
    assert acs(off, response(request_id), relay) == {"error": "identity_not_linked"}
    on = app(SAML={"okta": OKTA}, SIGNUP=True, FRONTEND_URL=FRONTEND)
    create_user("ann", email="ann@example.com")
    request_id, relay = start(on)
    assert acs(on, response(request_id), relay) == {"error": "identity_email_taken"}


def test_without_an_email_the_assertion_makes_no_account(app: App) -> None:
    """No EMAIL_ATTRIBUTE and a NameID not an email: nothing to sign up with."""
    plain = {key: value for key, value in OKTA.items() if key != "EMAIL_ATTRIBUTE"}
    client = app(SAML={"okta": plain}, SIGNUP=True, FRONTEND_URL=FRONTEND)
    request_id, relay = start(client)

    assert acs(client, response(request_id), relay) == {"error": "identity_not_linked"}


def test_metadata_describes_this_service_provider(app: App) -> None:
    """The entity id and the ACS, POST binding, assertions wanted signed."""
    client = app(SAML={"okta": OKTA}, FRONTEND_URL=FRONTEND)

    metadata = client.get("/auth/identity/saml/okta/metadata")

    root = etree.fromstring(metadata.content)
    assert metadata.headers["content-type"] == "application/samlmetadata+xml"
    assert root.get("entityID") == OKTA["ENTITY_ID"]
    assert root.find(".//{*}AssertionConsumerService").get("Location") == ACS
    assert client.get("/auth/identity/saml/nope/metadata").status_code == 404
    assert client.get("/auth/identity/providers").json()["saml"] == ["okta"]


def test_without_a_frontend_url_the_acs_refuses(app: App) -> None:
    """Nowhere to send the browser: 422."""
    client = app(SAML={"okta": OKTA}, SIGNUP=True)
    request_id, relay = start(client)

    answer = client.post(
        "/auth/identity/saml/okta/acs",
        data={"SAMLResponse": response(request_id), "RelayState": relay},
    )

    assert answer.status_code == 422


def test_a_saml_login_is_finished_only_in_the_browser_that_started_it(
    app: App,
) -> None:
    """The ACS and the ticket read the cookie the start set; another browser lacks it.

    The audit found an attacker's RelayState, or ticket, finished in the
    victim's browser, logging the victim into the attacker's account (A2-2).
    """
    client = app(SAML={"okta": OKTA}, SIGNUP=True, FRONTEND_URL=FRONTEND)
    request_id, relay = start(client)
    client.cookies.clear()  # Another browser: it never started this flow.
    elsewhere = acs(client, response(request_id), relay)

    request_id, relay = start(client)
    sent = acs(client, response(request_id), relay)
    kept = dict(client.cookies)
    client.cookies.clear()
    stolen = client.post("/auth/identity/ticket", json={"ticket": sent["ticket"]})
    client.cookies.update(kept)
    redeemed = client.post("/auth/identity/ticket", json={"ticket": sent["ticket"]})

    assert elsewhere == {"error": "saml_refused"}
    assert stolen.json()["code"] == "identity_not_linked"
    assert redeemed.status_code == 200, redeemed.json()
