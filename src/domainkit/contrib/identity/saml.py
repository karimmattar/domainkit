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
"""

import base64
import logging
import zlib
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import RedirectResponse

from domainkit.contrib.auth.integration import utc_now
from domainkit.exceptions import DomainkitError
from domainkit.http.access import Public

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
        claims = verified_claims(config, saml_response, f"_{flow.nonce}", utc_now())
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

    Raises:
        AssertionRefused: The response does not prove a login for this request.
    """
    from lxml import etree
    from signxml.exceptions import InvalidInput, InvalidSignature
    from signxml.verifier import XMLVerifier

    try:
        document = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise AssertionRefused("a SAMLResponse that is not base64") from None
    try:
        verified = XMLVerifier().verify(
            document, x509_cert=config.IDP_CERTIFICATE, expect_config=_expected()
        )
    except (InvalidSignature, InvalidInput, etree.XMLSyntaxError) as exc:
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
