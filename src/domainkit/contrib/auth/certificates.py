"""Client certificates (mutual TLS): a certificate someone checked names its user.

For services that call the API with a certificate instead of a token::

    AUTH = {
        "AUTHENTICATION_CLASSES": [
            "domainkit.contrib.auth.authentication.TokenAuthentication",
            "domainkit.contrib.auth.certificates.ClientCertificateAuthentication",
        ],
    }

The certificate comes from one of two places, and only these:

- the ASGI TLS extension (``scope["extensions"]["tls"]``), when the server
  terminates TLS and checked the certificate itself;
- ``AUTH.CLIENT_CERT.HEADER`` (URL-encoded PEM, nginx's
  ``$ssl_client_escaped_cert``), only on connections from
  ``AUTH.CLIENT_CERT.PROXIES``, the proxies that check certificates, and
  with ``VERIFY_HEADER`` saying ``SUCCESS`` when it is set. A request from
  anywhere else that sends the header is refused: a client could otherwise
  name any certificate. ``TRUSTED_PROXIES`` does not count: it often
  holds whole networks, where any host could send the header.

``MATCH = "fingerprint"`` (the default) finds the user by the SHA-256 of
the certificate, registered with ``addcertificate``; ``"subject"`` takes
the subject's common name as the username, which trusts everything the CA
signs (it needs ``cryptography``). A certificate that names no active user
is refused with 401. Neither this class nor domainkit checks the chain: the
server or the proxy does, and must be set to refuse certificates its CA did
not sign.
"""

import hashlib
import ipaddress
import ssl
from collections.abc import Iterable
from typing import Any, Final
from urllib.parse import unquote

from starlette.requests import HTTPConnection

from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.middleware import PEER

from .authentication import (
    AuthenticationFailed,
    BaseAuthentication,
    Credentials,
    active_user,
)
from .models import AbstractBaseUser
from .settings import auth_settings
from .tokens import TokenKind
from .uow import AuthUnitOfWork

SCHEME: Final = "certificate"
"""``request.auth.scheme`` of requests a client certificate authenticates."""
VERIFIED: Final = "SUCCESS"
"""What ``VERIFY_HEADER`` holds for a certificate the proxy checked."""

type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class ClientCertificateAuthentication(BaseAuthentication):
    """Users named by a client certificate the server or a trusted proxy checked."""

    async def authenticate(self, connection: HTTPConnection) -> Credentials | None:
        """The active user the request's client certificate names.

        Raises:
            AuthenticationFailed: The certificate was not checked, comes from
                an untrusted peer, is malformed, or names no active user.
        """
        pem = presented_certificate(connection)
        if pem is None:
            return None
        der = certificate_der(pem)
        async with AuthUnitOfWork() as uow:
            if auth_settings().CLIENT_CERT.MATCH == "fingerprint":
                fingerprint = hashlib.sha256(der).hexdigest()
                token = await uow.tokens.find_digest(fingerprint)
                if token is None or token.kind != TokenKind.CERTIFICATE:
                    raise AuthenticationFailed("Unknown client certificate.")
                user = await active_user(uow, token.user_id)
                return Credentials(user, scheme=SCHEME, token_id=token.id)
            user = await _subject_user(uow, der)
        return Credentials(user, scheme=SCHEME)


def presented_certificate(connection: HTTPConnection) -> str | None:
    """The client certificate in PEM, from the TLS extension or a proxy.

    Raises:
        AuthenticationFailed: The server did not verify it, or a header
            carries one from a peer not in ``CLIENT_CERT.PROXIES`` or
            without the proxy's check.
    """
    extensions: dict[str, Any] = connection.scope.get("extensions") or {}
    tls: dict[str, Any] = extensions.get("tls") or {}
    chain = tls.get("client_cert_chain") or ()
    if chain:
        if tls.get("client_cert_error") is not None:
            raise AuthenticationFailed("The client certificate did not verify.")
        first: str = chain[0]
        return first
    config = auth_settings().CLIENT_CERT
    value = connection.headers.get(config.HEADER) if config.HEADER else None
    if not value:
        return None
    if not trusted_peer(connection, config.PROXIES):
        raise AuthenticationFailed(
            "Client certificates in headers are taken from trusted proxies only."
        )
    if config.VERIFY_HEADER is not None and (
        connection.headers.get(config.VERIFY_HEADER) != VERIFIED
    ):
        raise AuthenticationFailed("The proxy did not verify the client certificate.")
    return unquote(value)


def trusted_peer(connection: HTTPConnection, proxies: Iterable[IPNetwork]) -> bool:
    """Whether the request came over a socket from one of ``proxies``.

    The peer is the one ``ProxyHeadersMiddleware`` kept, if it replaced
    ``client``, and ``client`` otherwise.
    """
    peer = connection.scope.get(PEER) or connection.scope.get("client")
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(peer[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in proxies)


def certificate_der(pem: str) -> bytes:
    """The certificate's DER bytes, from its PEM.

    Raises:
        AuthenticationFailed: It is not a PEM certificate.
    """
    try:
        return ssl.PEM_cert_to_DER_cert(pem.strip())
    except ValueError:
        raise AuthenticationFailed("Malformed client certificate.") from None


def fingerprint(pem: str) -> str:
    """The SHA-256 of a PEM certificate, in hex: how ``addcertificate`` stores it."""
    return hashlib.sha256(certificate_der(pem)).hexdigest()


def common_name(der: bytes) -> str | None:
    """The common name of the certificate's subject, if it has one.

    Raises:
        ImproperlyConfigured: ``cryptography`` is not installed.
        AuthenticationFailed: The bytes are not a certificate.
    """
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise ImproperlyConfigured(
            'MATCH = "subject" needs cryptography: pip install "domainkit[mfa]".'
        ) from exc
    try:
        certificate = x509.load_der_x509_certificate(der)
    except ValueError:
        raise AuthenticationFailed("Malformed client certificate.") from None
    names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(names[0].value) if names else None


async def _subject_user(uow: AuthUnitOfWork, der: bytes) -> AbstractBaseUser:
    """The active user whose username is the certificate's common name.

    Raises:
        AuthenticationFailed: There is none.
    """
    name = common_name(der)
    user = None if name is None else await uow.users.get_by_natural_key(name)
    if user is None or not user.is_active:
        raise AuthenticationFailed("Unknown client certificate.")
    return user
