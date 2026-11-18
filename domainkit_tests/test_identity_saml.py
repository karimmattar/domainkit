"""SAML 2.0 single sign-on: only what the provider signed, for this request."""

import asyncio
import base64
import threading
import zlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from lxml import etree
from signxml.signer import XMLSigner
from signxml.verifier import XMLVerifier

from domainkit.conf import override_settings
from domainkit.contrib.identity import saml
from domainkit.contrib.identity.providers import Claims
from domainkit.contrib.identity.settings import SamlSettings
from domainkit_tests.conftest import ExitStackClients, Migrated
from domainkit_tests.support.auth import bearer, create_user
from domainkit_tests.test_identity_providers import PROVIDERS

type App = Callable[..., TestClient]

FRONTEND = "https://app.example.com/login/done"
SIGNER = XMLSigner(c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#")
"""Exclusive canonicalization, as identity providers sign SAML."""
ACS = "https://api.example.com/auth/identity/saml/okta/acs"
XS = "http://www.w3.org/2001/XMLSchema"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
DS = "http://www.w3.org/2000/09/xmldsig#"


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
    attributes: str = "",
    **overrides: str,
) -> str:
    """A SAMLResponse, base64, signed as ``sign`` says.

    ``sign`` is ``response``, ``assertion`` or ``none``; ``attributes`` are
    added to the assertion's.
    """
    assertion = assertion_xml(request_id, **overrides).replace(
        "</saml:AttributeStatement>", f"{attributes}</saml:AttributeStatement>"
    )
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


def unsigned(names: int, *, extra: str = "") -> str:
    """A SAMLResponse of ``names`` distinct element names, signed by no one."""
    body = "".join(f"<n{index}x/>" for index in range(names))
    xml = (
        f'{extra}<samlp:Response xmlns:samlp="{saml.PROTOCOL}">{body}</samlp:Response>'
    )
    return base64.b64encode(xml.encode()).decode()


def test_the_acs_parses_each_response_in_a_thread_of_its_own(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never on the event loop's, nor on a thread that outlives it (audit A28-PERF-03).

    lxml keeps every name a thread parses for as long as the thread lives:
    on the loop's thread, each response of new names grew it for good, and
    blocked the loop while it parsed.
    """
    client = app(SAML={"okta": OKTA}, SIGNUP=True, FRONTEND_URL=FRONTEND)
    verified = saml.verified_claims
    seen: list[tuple[threading.Thread, bool]] = []

    def recording(*arguments: Any) -> Claims:  # noqa: ANN401 - verified_claims'.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen.append((threading.current_thread(), False))
        else:
            seen.append((threading.current_thread(), True))
        return verified(*arguments)

    monkeypatch.setattr(saml, "verified_claims", recording)
    for _ in "ab":
        request_id, relay = start(client)
        assert "ticket" in acs(client, response(request_id), relay)

    (first, on_a_loop), (second, again_on_a_loop) = seen
    first.join(5)
    second.join(5)
    assert (on_a_loop, again_on_a_loop) == (False, False)
    assert first is not second
    assert first.name == second.name == "domainkit.identity.saml"
    assert (first.is_alive(), second.is_alive()) == (False, False)


@pytest.mark.anyio
async def test_the_names_a_response_holds_go_with_its_thread() -> None:
    """The loop's thread keeps none of the names lxml parsed for it."""
    config = SamlSettings(**OKTA)
    now = datetime.now(UTC)
    before = etree.memory_debugger.dict_size()

    with pytest.raises(saml.AssertionRefused, match="no valid signature"):
        await saml._alone(
            lambda: saml.verified_claims(config, unsigned(5_000), "_x", now)
        )

    assert etree.memory_debugger.dict_size() == before


def test_a_response_past_its_bounds_is_refused_before_it_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characters, nodes as their bytes can make them, and a DTD; read as UTF-8.

    A DTD was refused once parsed; now before. Each ``<`` counts twice and
    each ``=`` once: a response at ``MAX_NODES`` is parsed, one past it is not.
    """
    config = SamlSettings(**OKTA)
    now = datetime.now(UTC)
    parsed: list[bytes] = []

    def verify(self: XMLVerifier, data: bytes, **_: Any) -> Any:  # noqa: ANN401
        parsed.append(data)
        raise etree.XMLSyntaxError("stop here", 1, 1, 1)

    monkeypatch.setattr(XMLVerifier, "verify", verify)
    # 2 * (names + 2) + 1 nodes: the root's tags, and its xmlns.
    at_the_bound = (saml.MAX_NODES - 5) // 2
    refused = [
        ("over 1,000,000 characters", "A" * (saml.MAX_RESPONSE + 4)),
        ("more than 50,000 nodes", unsigned(at_the_bound + 1)),
        ("a DTD or an entity", unsigned(1, extra="<!DOCTYPE r [<!ELEMENT r ANY>]>")),
        ("a DTD or an entity", unsigned(1, extra='<!entity x "y">')),
    ]
    for reason, encoded in refused:
        with pytest.raises(saml.AssertionRefused, match=reason):
            saml.verified_claims(config, encoded, "_x", now)
    with pytest.raises(saml.AssertionRefused, match="no valid signature"):
        saml.verified_claims(config, unsigned(at_the_bound), "_x", now)

    assert parsed == [base64.b64decode(unsigned(at_the_bound))]


def test_a_response_is_read_as_utf_8(app: App) -> None:
    """With a byte order mark too; in UTF-16, or Latin-1 past ASCII, refused.

    Each is signed as identity providers sign: its canonical form is the
    same, in UTF-8, so 2.8 believed all three.
    """
    client = app(SAML={"okta": OKTA}, SIGNUP=True, FRONTEND_URL=FRONTEND)

    def utf_8_marked(request_id: str) -> str:
        signed = base64.b64decode(response(request_id))
        return base64.b64encode(b"\xef\xbb\xbf" + signed).decode()

    def utf_16(request_id: str) -> str:
        signed = base64.b64decode(response(request_id)).decode()
        declared = f'<?xml version="1.0" encoding="UTF-16"?>{signed}'
        return base64.b64encode(declared.encode("utf-16")).decode()

    def latin_1(request_id: str) -> str:
        signed = base64.b64decode(response(request_id, subject="00u\u00e9")).decode()
        written = signed.replace("&#233;", "\u00e9")  # lxml wrote a reference.
        declared = f'<?xml version="1.0" encoding="ISO-8859-1"?>{written}'
        return base64.b64encode(declared.encode("latin-1")).decode()

    sent = []
    for encoded in (utf_8_marked, utf_16, latin_1):
        request_id, relay = start(client)
        sent.append(acs(client, encoded(request_id), relay))

    assert "ticket" in sent[0]
    assert sent[1:] == [{"error": "saml_refused"}] * 2


@pytest.mark.anyio
async def test_parses_at_once_are_bounded_and_a_cancelled_one_keeps_its_slot() -> None:
    """``PARSES`` at once on a loop; the others wait for a thread to end.

    A request cancelled while its response is parsed holds its slot until
    the thread ends: the thread runs on, and counts.
    """
    claims = Claims(subject="00u1", email=None, email_verified=False)
    release = threading.Event()
    started = threading.Semaphore(0)
    running: list[int] = []

    def work() -> Claims:
        running.append(1)
        started.release()
        release.wait(10)
        return claims

    tasks = [asyncio.create_task(saml._alone(work)) for _ in range(saml.PARSES + 1)]
    for _ in range(saml.PARSES):
        assert await asyncio.to_thread(started.acquire, timeout=10)
    await asyncio.sleep(0.05)
    waiting = len(running)
    tasks[0].cancel()
    await asyncio.sleep(0.05)
    still = len(running)
    release.set()
    answers = await asyncio.gather(*tasks[1:])

    assert waiting == still == saml.PARSES
    assert tasks[0].cancelled()
    assert answers == [claims] * saml.PARSES
    assert len(running) == saml.PARSES + 1


def signature(references: int = 1) -> str:
    """A ``ds:Signature`` of ``references`` references, that no one signed.

    Its ``SignedInfo`` holds 3 elements, and 3 more a reference.
    """
    reference = (
        '<ds:Reference URI=""><ds:DigestMethod Algorithm="http://www.w3.org/2001/'
        '04/xmlenc#sha256"/><ds:DigestValue>AAAA</ds:DigestValue></ds:Reference>'
    )
    return (
        f'<ds:Signature xmlns:ds="{DS}"><ds:SignedInfo><ds:CanonicalizationMethod '
        'Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"/>'
        '<ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#'
        f'rsa-sha256"/>{reference * references}</ds:SignedInfo>'
        "<ds:SignatureValue>AAAA</ds:SignatureValue></ds:Signature>"
    )


def declaring(count: int, *, nested: int = 1, inside: str = "") -> str:
    """A response whose elements declare ``count`` namespaces, over ``nested`` levels.

    ``samlp`` and ``ds`` are in scope besides, where ``inside`` is.
    """
    per = count // nested
    opened = [
        "<e "
        + " ".join(
            f'xmlns:p{level}x{index}="urn:p{level}x{index}"' for index in range(per)
        )
        + ">"
        for level in range(nested)
    ]
    xml = (
        f'<samlp:Response xmlns:samlp="{saml.PROTOCOL}">{"".join(opened)}{inside}'
        f"{'</e>' * nested}</samlp:Response>"
    )
    return base64.b64encode(xml.encode()).decode()


def test_canonicalization_is_bounded_before_signxml_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Namespaces in scope on an element, and a SignedInfo's elements (lead review).

    Counted before signxml reads the response, within ``MAX_NODES``: a
    prefix counts once however often it is declared, so 32 in scope pass,
    flat or nested, and a ``SignedInfo`` of 32 elements does.
    """
    config = SamlSettings(**OKTA)
    now = datetime.now(UTC)
    parsed: list[bytes] = []

    def verify(self: XMLVerifier, data: bytes, **_: Any) -> Any:  # noqa: ANN401
        parsed.append(data)
        raise etree.XMLSyntaxError("stop here", 1, 1, 1)

    monkeypatch.setattr(XMLVerifier, "verify", verify)
    refused = [
        ("more than 32 namespaces", declaring(31, inside=signature())),
        ("more than 32 namespaces", declaring(12_000, inside=signature(40))),
        ("more than 32 namespaces", declaring(40, nested=10, inside=signature())),
        ("more than 32 elements", declaring(0, inside=signature(10))),
    ]
    for reason, encoded in refused:
        with pytest.raises(saml.AssertionRefused, match=reason):
            saml.verified_claims(config, encoded, "_x", now)
    passed = [
        declaring(30, inside=signature()),
        declaring(30, nested=10, inside=signature()),
        declaring(0, inside=signature(9)),
        declaring(0, nested=200, inside=signature()),
    ]
    for encoded in passed:
        with pytest.raises(saml.AssertionRefused, match="no valid signature"):
            saml.verified_claims(config, encoded, "_x", now)

    assert parsed == [base64.b64decode(each) for each in passed]


def test_an_okta_response_that_declares_namespaces_again_logs_in(app: App) -> None:
    """``xs`` and ``xsi`` on each of 500 group values: 1,000 declared, 2 in scope."""
    client = app(SAML={"okta": OKTA}, SIGNUP=True, FRONTEND_URL=FRONTEND)
    request_id, relay = start(client)
    values = "".join(
        f'<saml:AttributeValue xmlns:xs="{XS}" xmlns:xsi="{XSI}" '
        f'xsi:type="xs:string">group-{index}</saml:AttributeValue>'
        for index in range(500)
    )
    groups = f'<saml:Attribute Name="groups">{values}</saml:Attribute>'
    encoded = response(request_id, attributes=groups)

    assert base64.b64decode(encoded).count(b"xmlns:xsi=") == 500
    assert "ticket" in acs(client, encoded, relay)


VALUE: Final = "<ds:SignatureValue>AAAA</ds:SignatureValue>"


@pytest.mark.parametrize(
    ("declared", "inside"),
    [
        ('xmlns:r="relative/uri"', signature()),
        ("", signature().replace(VALUE, "")),
        ("", signature().replace(VALUE, "<ds:SignatureValue/>")),
    ],
    ids=["a relative namespace", "no signature value", "an empty signature value"],
)
def test_what_lxml_or_signxml_raise_of_a_response_refuses_it(
    app: App, declared: str, inside: str
) -> None:
    """Canonicalization, the signature's schema, signxml's own reading: never a 500.

    lxml's ``C14NError`` and ``DocumentInvalid``, and signxml's ``TypeError``
    on an empty value, reached the ACS as a 500 (lead review).
    """
    client = app(SAML={"okta": OKTA}, SIGNUP=True, FRONTEND_URL=FRONTEND)
    _, relay = start(client)
    root = f'<samlp:Response {declared} xmlns:samlp="{saml.PROTOCOL}">'
    xml = f"{root}{inside}</samlp:Response>"

    sent = acs(client, base64.b64encode(xml.encode()).decode(), relay)

    assert sent == {"error": "saml_refused"}


@pytest.mark.anyio
async def test_a_response_that_waits_too_long_for_a_slot_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past ``SLOT_WAIT``, refused: a parse under way never holds others for long."""
    monkeypatch.setattr(saml, "SLOT_WAIT", 0.2)
    release = threading.Event()
    started = threading.Semaphore(0)

    def work() -> Claims:
        started.release()
        release.wait(10)
        return Claims(subject="00u1", email=None, email_verified=False)

    held = [asyncio.create_task(saml._alone(work)) for _ in range(saml.PARSES)]
    for _ in range(saml.PARSES):
        assert await asyncio.to_thread(started.acquire, timeout=10)
    try:
        with pytest.raises(saml.AssertionRefused, match="no slot free"):
            await saml._alone(work)
    finally:
        release.set()
        await asyncio.gather(*held)
