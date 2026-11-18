"""SAML 2.0 single sign-on, as a service provider (``domainkit[saml]``).

::

    IDENTITY = {
        "SAML": {
            "okta": {
                "ENTITY_ID": "https://api.example.com/saml",
                "ACS_URL": "https://api.example.com/auth/identity/saml/okta/acs",
                "IDP_ENTITY_ID": "http://www.okta.com/exk...",
                "IDP_SSO_URL": "https://acme.okta.com/app/.../sso/saml",
                "IDP_CERTIFICATE": env.str("OKTA_CERTIFICATE"),
            },
        },
        "FRONTEND_URL": "https://app.example.com/login/done",
    }

``GET /identity/saml/{name}/login`` sends the browser to the provider
with an AuthnRequest (HTTP-Redirect binding); its id and the RelayState
are a flow's, kept server-side and used once. The provider posts its
response to ``/identity/saml/{name}/acs``, which believes only what is
signed with ``IDP_CERTIFICATE``: the signed element signxml returns, so
an unsigned assertion slipped beside a signed one (signature wrapping)
is never read. The assertion must name the provider as issuer, this
application as audience and recipient, answer this request
(``InResponseTo``), and be within its times. The browser is then sent
to ``FRONTEND_URL`` with a one-time ``ticket``, which the page trades
for tokens at ``/identity/ticket``, or with an ``error`` code.
Encrypted assertions are not read.

Anyone may post to the ACS, so its response is bounded before signxml
reads it (since 2.9): ``MAX_RESPONSE`` base64 characters, and
``MAX_NODES`` nodes, counted on its bytes as ``domainkit.documents``
counts a Word document's (each ``<`` twice, each ``=`` once). A DTD or
an entity is refused before parsing, and it is read as UTF-8 only, so
the count sees what is parsed. It is then read once for what makes
canonicalization cost: ``MAX_NAMESPACES`` namespaces in scope and
``MAX_ATTRIBUTES`` attributes on an element, and ``MAX_SIGNED_INFO``
elements and ``MAX_SIGNED_INFO_BYTES`` bytes in a ``SignedInfo``. It is
parsed in a thread of its own, which ends with it and takes with it the
names lxml keeps for the thread, never on the event loop's; ``PARSES``
at most at once on a loop, the others waiting ``SLOT_WAIT`` seconds at
most.
"""

import asyncio
import base64
import contextlib
import contextvars
import io
import logging
import re
import threading
import zlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import RedirectResponse

from domainkit.contrib.auth.integration import utc_now
from domainkit.exceptions import DomainkitError
from domainkit.http.access import Public
from domainkit.utils.loops import PerLoop

from .binding import COOKIE, TICKET_SEPARATOR, binding, bound
from .exceptions import EmailTaken, NoAccount, UnknownProvider
from .providers import Claims
from .services import LOGIN, account_for, identity_settings
from .settings import SamlSettings
from .stores import IdentityUnitOfWork

if TYPE_CHECKING:
    from signxml.verifier import SignatureConfiguration

__all__ = ["router"]

logger = logging.getLogger(__name__)
router = APIRouter(tags=["identity"])

PROTOCOL: Final = "urn:oasis:names:tc:SAML:2.0:protocol"
ASSERTION: Final = "urn:oasis:names:tc:SAML:2.0:assertion"
METADATA: Final = "urn:oasis:names:tc:SAML:2.0:metadata"
POST_BINDING: Final = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
SUCCESS: Final = "urn:oasis:names:tc:SAML:2.0:status:Success"
EMAIL_FORMAT: Final = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
SKEW: Final = timedelta(seconds=60)
MAX_RESPONSE: Final = 1_000_000
"""The largest SAMLResponse read, in base64 characters."""
MAX_NODES: Final = 50_000
"""The most nodes a SAMLResponse's XML may make, counted before it is parsed.

A thousand group values make some 7,000."""
MAX_NAMESPACES: Final = 32
"""The most namespaces in scope on an element: its prefixes, and the default.

Identity providers write fewer than ten, repeated on many elements
(``xmlns:xs`` on each attribute value) as they may be."""
MAX_ATTRIBUTES: Final = 32
"""The most attributes an element may hold, its namespace declarations aside.

Identity providers write up to some seven (a response's ``ID``,
``Version``, ``IssueInstant``, ``Destination``, ``InResponseTo``)."""
MAX_SIGNED_INFO: Final = 32
"""The most elements a ``ds:SignedInfo`` may hold, itself included.

One reference, its transforms, and their methods take some ten."""
MAX_SIGNED_INFO_BYTES: Final = 16 * 1024
"""The most bytes a ``ds:SignedInfo`` may take, serialized.

Identity providers write under 2 KiB."""
PARSES: Final = 4
"""The most responses parsed at once on one event loop."""
SLOT_WAIT: Final = 5.0
"""How long, in seconds, a response waits for one of the ``PARSES`` slots
before it is refused."""
SIGNED_INFO: Final = "{http://www.w3.org/2000/09/xmldsig#}SignedInfo"
DECLARATION: Final = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)
"""What declares a DTD or an entity: refused before the XML is parsed."""
NS: Final = {"samlp": PROTOCOL, "saml": ASSERTION}


class AssertionRefused(DomainkitError):
    """The response does not prove a login; the reason is logged, never shown."""


def saml_settings(name: str) -> SamlSettings:
    """The SAML provider ``name``.

    Raises:
        UnknownProvider: ``IDENTITY.SAML`` has none of that name.
    """
    config = identity_settings().SAML.get(name)
    if config is None:
        raise UnknownProvider("No identity provider has that name.")
    return config


def provider_key(name: str) -> str:
    """How identities and flows of the SAML provider ``name`` are recorded."""
    return f"saml:{name}"


@router.get("/identity/saml/{name}/login", dependencies=[Public])
async def saml_login(name: str) -> RedirectResponse:
    """Send the browser to the provider with an AuthnRequest."""
    config = saml_settings(name)
    async with IdentityUnitOfWork() as uow:
        await uow.flows.clear_expired()
        flow = await uow.flows.start(
            provider_key(name), LOGIN, None, identity_settings().FLOW_TTL
        )
        await uow.commit()
    request = authn_request(config, f"_{flow.nonce}", utc_now())
    encoded = base64.b64encode(_deflate(request.encode())).decode()
    query = urlencode({"SAMLRequest": encoded, "RelayState": flow.id})
    separator = "&" if "?" in config.IDP_SSO_URL else "?"
    sent = RedirectResponse(
        f"{config.IDP_SSO_URL}{separator}{query}", status_code=status.HTTP_302_FOUND
    )
    # The flow is this browser's: the ACS, posted from the provider's page
    # (another site, hence SameSite=None), and the ticket read it back.
    sent.set_cookie(
        COOKIE,
        binding(flow.id),
        max_age=int(identity_settings().FLOW_TTL.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="none",
    )
    return sent


@router.post("/identity/saml/{name}/acs", dependencies=[Public])
async def saml_acs(
    name: str,
    request: Request,
    saml_response: str = Form(alias="SAMLResponse", max_length=MAX_RESPONSE),
    relay_state: str = Form(alias="RelayState", max_length=64),
) -> RedirectResponse:
    """Check the provider's response; send the browser on with a ticket or an error."""
    config = saml_settings(name)
    frontend = identity_settings().FRONTEND_URL
    if frontend is None:
        raise NoAccount("IDENTITY.FRONTEND_URL is unset: nowhere to send the browser.")
    async with IdentityUnitOfWork() as uow:
        flow = await uow.flows.take(relay_state)
        await uow.commit()
    try:
        if flow is None or flow.provider != provider_key(name):
            raise AssertionRefused("an unknown, used, or expired RelayState")
        if not bound(flow.id, request.cookies.get(COOKIE)):
            raise AssertionRefused("a RelayState another browser started")
        request_id, now = f"_{flow.nonce}", utc_now()
        claims = await _alone(
            lambda: verified_claims(config, saml_response, request_id, now)
        )
        user = await account_for(provider_key(name), claims)
    except AssertionRefused as exc:
        # A configured provider's name; line breaks dropped all the same.
        shown = name.replace("\r", "").replace("\n", "")
        logger.warning("A SAML response from %s was refused: %s.", shown, exc)
        return _onward(frontend, error="saml_refused")
    except (NoAccount, EmailTaken) as exc:
        return _onward(frontend, error=exc.code)
    async with IdentityUnitOfWork() as uow:
        ticket = await uow.tickets.issue(user.id, identity_settings().TICKET_TTL)
        await uow.commit()
    # The flow rides along, so the redemption checks this browser's cookie.
    return _onward(frontend, ticket=f"{ticket}{TICKET_SEPARATOR}{flow.id}")


@router.get("/identity/saml/{name}/metadata", dependencies=[Public])
async def saml_metadata(name: str) -> Response:
    """This service provider's metadata, for the identity provider."""
    config = saml_settings(name)
    xml = (
        f'<md:EntityDescriptor xmlns:md="{METADATA}" '
        f'entityID="{_attr(config.ENTITY_ID)}">'
        f'<md:SPSSODescriptor protocolSupportEnumeration="{PROTOCOL}" '
        'AuthnRequestsSigned="false" WantAssertionsSigned="true">'
        f'<md:AssertionConsumerService Binding="{POST_BINDING}" '
        f'Location="{_attr(config.ACS_URL)}" index="0"/>'
        "</md:SPSSODescriptor></md:EntityDescriptor>"
    )
    return Response(xml, media_type="application/samlmetadata+xml")


def authn_request(config: SamlSettings, request_id: str, now: datetime) -> str:
    """An AuthnRequest asking for a POSTed response to the ACS URL."""
    return (
        f'<samlp:AuthnRequest xmlns:samlp="{PROTOCOL}" xmlns:saml="{ASSERTION}" '
        f'ID="{request_id}" Version="2.0" IssueInstant="{_instant(now)}" '
        f'Destination="{_attr(config.IDP_SSO_URL)}" '
        f'AssertionConsumerServiceURL="{_attr(config.ACS_URL)}" '
        f'ProtocolBinding="{POST_BINDING}">'
        f"<saml:Issuer>{_attr(config.ENTITY_ID)}</saml:Issuer>"
        '<samlp:NameIDPolicy AllowCreate="true"/>'
        "</samlp:AuthnRequest>"
    )


def verified_claims(
    config: SamlSettings, encoded: str, request_id: str, now: datetime
) -> Claims:
    """Who the signed assertion of ``encoded`` says logged in.

    Bounded before it is parsed: ``MAX_RESPONSE`` characters, ``MAX_NODES``
    nodes, no DTD or entity; read as UTF-8. Then bounded before signxml
    reads it: ``MAX_NAMESPACES``, ``MAX_ATTRIBUTES``, ``MAX_SIGNED_INFO``
    and ``MAX_SIGNED_INFO_BYTES`` (``_shaped``).
    CPU-bound, and lxml keeps each name it parses for the thread's life:
    the ACS calls it in a thread of its own.

    Raises:
        AssertionRefused: The response does not prove a login for this
            request; what lxml or signxml raise of it among them.
    """
    from lxml import etree
    from signxml.exceptions import SignXMLException
    from signxml.verifier import XMLVerifier

    if len(encoded) > MAX_RESPONSE:
        raise AssertionRefused(f"a SAMLResponse over {MAX_RESPONSE:,} characters")
    try:
        document = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise AssertionRefused("a SAMLResponse that is not base64") from None
    if DECLARATION.search(document):
        raise AssertionRefused("a SAMLResponse that declares a DTD or an entity")
    if 2 * document.count(b"<") + document.count(b"=") > MAX_NODES:
        raise AssertionRefused(f"a SAMLResponse of more than {MAX_NODES:,} nodes")
    try:
        _shaped(document)
        verified = XMLVerifier().verify(
            document,
            x509_cert=config.IDP_CERTIFICATE,
            expect_config=_expected(),
            # signxml's own parser, read as UTF-8 whatever the XML declares.
            parser=etree.XMLParser(resolve_entities=False, encoding="utf-8"),
        )
    except (SignXMLException, etree.LxmlError, ValueError, TypeError) as exc:
        # A schema it fails, a namespace canonicalization refuses, a value
        # that is not base64, an empty one signxml reads as None: the
        # response proves nothing.
        raise AssertionRefused(f"no valid signature ({type(exc).__name__})") from None
    signed = verified.signed_xml  # type: ignore[union-attr]
    if signed is None:
        raise AssertionRefused("nothing signed")  # pragma: no cover - signxml says.
    if etree.QName(signed).localname == "Response":
        status_code = signed.find("samlp:Status/samlp:StatusCode", NS)
        if status_code is None or status_code.get("Value") != SUCCESS:
            raise AssertionRefused("a response that is not a success")
        assertion = signed.find("saml:Assertion", NS)
    elif etree.QName(signed).localname == "Assertion":
        assertion = signed
    else:
        raise AssertionRefused("a signed element that is no response or assertion")
    if assertion is None:
        raise AssertionRefused("no assertion (an encrypted one is not read)")
    return _claims(config, assertion, request_id, now)


def _claims(
    config: SamlSettings, assertion: object, request_id: str, now: datetime
) -> Claims:
    """The checks of a signed assertion, then its subject and email."""
    find = assertion.find  # type: ignore[attr-defined]
    issuer = find("saml:Issuer", NS)
    if issuer is None or (issuer.text or "").strip() != config.IDP_ENTITY_ID:
        raise AssertionRefused("another issuer")
    conditions = find("saml:Conditions", NS)
    if conditions is None:
        raise AssertionRefused("no conditions")
    _within(conditions.get("NotBefore"), conditions.get("NotOnOrAfter"), now)
    audiences = {
        (each.text or "").strip()
        for each in conditions.iterfind("saml:AudienceRestriction/saml:Audience", NS)
    }
    if config.ENTITY_ID not in audiences:
        raise AssertionRefused("another audience")
    subject = find("saml:Subject", NS)
    name_id = None if subject is None else subject.find("saml:NameID", NS)
    if name_id is None or not (name_id.text or "").strip():
        raise AssertionRefused("no subject")
    confirmed = [
        data
        for data in subject.iterfind(
            "saml:SubjectConfirmation/saml:SubjectConfirmationData", NS
        )
        if data.get("InResponseTo") == request_id
        and data.get("Recipient") == config.ACS_URL
    ]
    if not confirmed:
        raise AssertionRefused("an answer to another request, or for another recipient")
    _within(None, confirmed[0].get("NotOnOrAfter"), now)
    nameid = name_id.text.strip()
    email = None
    if config.EMAIL_ATTRIBUTE:
        statement = find("saml:AttributeStatement", NS)
        for attribute in [] if statement is None else statement:
            if attribute.get("Name") == config.EMAIL_ATTRIBUTE:
                value = attribute.find("saml:AttributeValue", NS)
                email = None if value is None else (value.text or "").strip() or None
    elif name_id.get("Format") == EMAIL_FORMAT:
        email = nameid
    return Claims(subject=nameid, email=email, email_verified=email is not None)


def _shaped(document: bytes) -> None:
    """Refuse a response whose canonicalization would cost, before signxml runs.

    Read once, as signxml's parser reads it (UTF-8, no entity): each
    element's namespaces in scope, each prefix once however often it is
    declared, and its attributes, which canonicalization sorts, in the
    signed part signxml canonicalizes before it checks a digest as well as
    in a ``SignedInfo``; and each ``SignedInfo``'s elements and bytes,
    which signxml canonicalizes before it checks a signature.

    Raises:
        AssertionRefused: An element has more than ``MAX_NAMESPACES`` in
            scope or ``MAX_ATTRIBUTES`` attributes, or a ``SignedInfo``
            more than ``MAX_SIGNED_INFO`` elements or
            ``MAX_SIGNED_INFO_BYTES`` bytes.
        lxml.etree.XMLSyntaxError: It is not well-formed.
    """
    from lxml import etree

    scopes: list[frozenset[str]] = [frozenset()]
    declared: list[str] = []
    signed, opened = 0, -1  # The open SignedInfo's elements, and its depth.
    for event, item in etree.iterparse(
        io.BytesIO(document),
        events=("start-ns", "start", "end"),
        encoding="utf-8",
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
    ):
        if event == "start-ns":
            declared.append(item[0])
        elif event == "start":
            scope = scopes[-1].union(declared) if declared else scopes[-1]
            declared = []
            if len(scope) > MAX_NAMESPACES:
                raise AssertionRefused(
                    f"an element with more than {MAX_NAMESPACES} namespaces in scope"
                )
            if len(item.attrib) > MAX_ATTRIBUTES:
                raise AssertionRefused(
                    f"an element with more than {MAX_ATTRIBUTES} attributes"
                )
            scopes.append(scope)
            if signed or item.tag == SIGNED_INFO:
                opened = len(scopes) - 1 if not signed else opened
                signed += 1
                if signed > MAX_SIGNED_INFO:
                    raise AssertionRefused(
                        f"a SignedInfo of more than {MAX_SIGNED_INFO} elements"
                    )
        else:
            scopes.pop()
            if len(scopes) == opened:  # The SignedInfo ends, read whole.
                size = len(etree.tostring(item, with_tail=False))
                if size > MAX_SIGNED_INFO_BYTES:
                    raise AssertionRefused(
                        f"a SignedInfo of more than {MAX_SIGNED_INFO_BYTES:,} bytes"
                    )
                signed, opened = 0, -1
            if not signed:  # A SignedInfo's elements are kept until it ends.
                item.clear()


_parsing: Final[PerLoop[asyncio.Semaphore]] = PerLoop()
"""Each loop's slots for responses parsed at once (``PARSES``)."""


async def _alone(work: Callable[[], Claims]) -> Claims:
    """``work()``, in a thread of its own that ends with it; ``PARSES`` at once.

    lxml keeps every name a thread parses for the thread's life: on the
    event loop's thread, each response of new names would grow it for good,
    and block every other request while it parsed (ADR 0181 did the same
    for Word documents). A slot is held until the thread ends, even if the
    request is cancelled meanwhile; one not free within ``SLOT_WAIT``
    seconds refuses the response. ``work`` runs in a copy of the context.

    The thread is not a daemon: a process that exits while a response is
    parsed waits for the parse, which the bounds keep short, rather than
    stop it halfway through lxml's or OpenSSL's code.

    Raises:
        AssertionRefused: What ``work`` raised, raised again here; or no
            slot was free in time.
    """
    loop = asyncio.get_running_loop()
    slots = _parsing.get(lambda: asyncio.Semaphore(PARSES))
    try:
        async with asyncio.timeout(SLOT_WAIT):
            await slots.acquire()
    except TimeoutError:
        raise AssertionRefused(
            f"no slot free to parse it in within {SLOT_WAIT:g} seconds"
        ) from None
    done: asyncio.Future[Claims] = loop.create_future()
    context = contextvars.copy_context()

    def settle(result: Claims | None, error: BaseException | None) -> None:
        slots.release()
        if done.done():  # The request was cancelled meanwhile.
            return
        if error is not None:
            done.set_exception(error)
        else:
            done.set_result(cast("Claims", result))

    def run() -> None:
        result, error = None, None
        try:
            result = context.run(work)
        except BaseException as raised:  # noqa: BLE001 - raised again in the caller.
            error = raised
        with contextlib.suppress(RuntimeError):  # The loop closed meanwhile.
            loop.call_soon_threadsafe(settle, result, error)

    try:
        threading.Thread(target=run, name="domainkit.identity.saml").start()
    except BaseException:  # pragma: no cover - no thread could start.
        slots.release()
        raise
    return await done


def _expected() -> "SignatureConfiguration":
    """Signatures believed: RSA or ECDSA, SHA-256 or stronger, one reference."""
    from signxml.algorithms import DigestAlgorithm as D
    from signxml.algorithms import SignatureMethod as M
    from signxml.verifier import SignatureConfiguration

    return SignatureConfiguration(
        signature_methods=frozenset(
            {
                M.RSA_SHA256,
                M.RSA_SHA384,
                M.RSA_SHA512,
                M.ECDSA_SHA256,
                M.ECDSA_SHA384,
                M.ECDSA_SHA512,
            }
        ),
        digest_algorithms=frozenset({D.SHA256, D.SHA384, D.SHA512}),
    )


def _within(not_before: str | None, not_after: str | None, now: datetime) -> None:
    """Refuse an assertion outside its times, give or take the skew."""
    try:
        if not_before is not None and now + SKEW < _parse(not_before):
            raise AssertionRefused("not yet valid")
        if not_after is None or now - SKEW >= _parse(not_after):
            raise AssertionRefused("expired")
    except ValueError:
        raise AssertionRefused("an unreadable time") from None


def _parse(text: str) -> datetime:
    """An xs:dateTime in UTC."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _instant(moment: datetime) -> str:
    """An xs:dateTime, as SAML writes it."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _deflate(data: bytes) -> bytes:
    """Raw DEFLATE, as the HTTP-Redirect binding wants."""
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(data) + compressor.flush()


def _attr(text: str) -> str:
    """``text`` escaped for an XML attribute or element."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _onward(frontend: str, **query: str) -> RedirectResponse:
    """See Other to the page, with ``query``."""
    separator = "&" if "?" in frontend else "?"
    return RedirectResponse(
        f"{frontend}{separator}{urlencode(query)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
