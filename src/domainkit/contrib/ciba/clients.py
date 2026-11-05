"""CIBA clients: registered by staff, authenticated on every request.

A client is registered with ``domainkit ciba add`` (or ``register``). It
gets a generated ``client_id``, a name the phone shows, the permission
keys its tokens may carry, and one way to prove itself:

- ``client_secret_basic`` or ``client_secret_post``: a generated 256-bit
  secret, shown once and kept as its SHA-256.
- ``private_key_jwt`` (FAPI asks for it): the client's public keys (a JWK
  Set); it signs a short-lived assertion (RFC 7523) with an asymmetric
  algorithm, naming itself in ``iss`` and ``sub``, the provider in
  ``aud``, with an ``exp`` at most ``ASSERTION_MAX_AGE`` away and a
  ``jti`` that works once. It needs PyJWT (the ``jwt`` extra).
"""

import base64
import binascii
import hmac
import json
import secrets
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import unquote_plus

from starlette.requests import Request

from domainkit.contrib.auth.integration import utc_now
from domainkit.exceptions import ImproperlyConfigured

from .exceptions import OAuthError, invalid_client
from .services import ciba_settings, secret_digest
from .stores import CibaClient, CibaUnitOfWork

__all__ = [
    "ASSERTION_TYPE",
    "METHODS",
    "authenticate_client",
    "register",
    "rotate_secret",
]

CLIENT_SECRET_BASIC: Final = "client_secret_basic"  # noqa: S105 - a method name.
CLIENT_SECRET_POST: Final = "client_secret_post"  # noqa: S105 - a method name.
PRIVATE_KEY_JWT: Final = "private_key_jwt"
METHODS: Final = (CLIENT_SECRET_BASIC, CLIENT_SECRET_POST, PRIVATE_KEY_JWT)
"""How clients prove who they are (``token_endpoint_auth_methods_supported``)."""
ASSERTION_TYPE: Final = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
"""The ``client_assertion_type`` of ``private_key_jwt`` (RFC 7523)."""
ASSERTION_ALGORITHMS: Final = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)
"""Asymmetric algorithms only: a shared secret would sign as the provider can."""
ASSERTION_LENGTH: Final = 8192
"""Characters an assertion may have."""
JTI_LENGTH: Final = 255
SKEW: Final = timedelta(seconds=30)
"""How far a client's clock may be ahead of the provider's."""
SECRET_BYTES: Final = 32


def _client_id() -> str:
    """A new client id: 128 random bits, URL-safe."""
    return f"ciba_{secrets.token_urlsafe(16)}"


def _secret() -> str:
    """A new client secret: 256 random bits, URL-safe."""
    return secrets.token_urlsafe(SECRET_BYTES)


def _checked_jwks(jwks: str) -> str:
    """``jwks`` as a JWK Set of public keys PyJWT reads.

    Raises:
        ImproperlyConfigured: PyJWT is missing, or the set holds no public
            key, or a private one.
    """
    try:
        import jwt
    except ImportError:  # pragma: no cover - the jwt extra is installed in tests.
        raise ImproperlyConfigured(
            "private_key_jwt needs PyJWT: pip install 'domainkit[jwt]'."
        ) from None
    try:
        loaded = json.loads(jwks)
        keys = jwt.PyJWKSet.from_dict(loaded).keys
    except (ValueError, jwt.PyJWTError) as exc:
        raise ImproperlyConfigured(f"The JWK Set does not read: {exc}") from None
    if (
        any("d" in key or key.get("kty") == "oct" for key in loaded.get("keys", ()))
        or not keys
    ):
        raise ImproperlyConfigured(
            "Give the client's public keys only: no private or shared key."
        )
    return json.dumps(loaded, separators=(",", ":"))


async def register(
    name: str, *, scopes: Collection[str] = (), method: str, jwks: str | None = None
) -> tuple[CibaClient, str | None]:
    """Register a client; the client, and its secret (shown once) if it has one.

    Raises:
        ImproperlyConfigured: ``method`` is unknown; ``private_key_jwt``
            without a usable ``jwks``, or a secret method with one.
    """
    if method not in METHODS:
        raise ImproperlyConfigured(f"method is one of {', '.join(METHODS)}.")
    if (method == PRIVATE_KEY_JWT) != (jwks is not None):
        raise ImproperlyConfigured(
            "private_key_jwt takes the client's JWK Set; the secret methods none."
        )
    secret = None if jwks is not None else _secret()
    client = CibaClient(
        client_id=_client_id(),
        name=name,
        auth_method=method,
        secret_digest=None if secret is None else secret_digest(secret),
        jwks=None if jwks is None else _checked_jwks(jwks),
        scopes=" ".join(sorted(set(scopes))),
        created_at=utc_now(),
    )
    async with CibaUnitOfWork() as uow:
        await uow.clients.add(client)
        await uow.commit()
    return client, secret


async def rotate_secret(client_id: str) -> str:
    """A new secret for the client; the old one stops working now.

    Raises:
        ImproperlyConfigured: No client has the id, or it signs assertions.
    """
    async with CibaUnitOfWork() as uow:
        client = await uow.clients.get(client_id)
        if client is None or client.secret_digest is None:
            raise ImproperlyConfigured(f"No client with a secret has id {client_id!r}.")
        secret = _secret()
        await uow.clients.replace(client, secret_digest=secret_digest(secret))
        await uow.commit()
    return secret


async def authenticate_client(
    request: Request, form: Mapping[str, str], *, audiences: Collection[str]
) -> CibaClient:
    """The client the request proves it is, by the one method it uses.

    ``audiences`` are the values an assertion's ``aud`` may hold: the
    issuer, and the endpoint's URL.

    Raises:
        OAuthError: ``invalid_request`` for two methods at once;
            ``invalid_client`` for anything that does not prove a client.
    """
    basic = _basic(request)
    assertion = form.get("client_assertion")
    used = [
        method
        for method, present in (
            (CLIENT_SECRET_BASIC, basic is not None),
            (CLIENT_SECRET_POST, "client_secret" in form),
            (PRIVATE_KEY_JWT, assertion is not None),
        )
        if present
    ]
    if len(used) > 1:
        raise OAuthError("invalid_request", "Authenticate the client one way only.")
    if not used:
        raise invalid_client("Authenticate the client.")
    if basic is not None:
        return await _by_secret(*basic, CLIENT_SECRET_BASIC, form)
    if assertion is None:
        return await _by_secret(
            form.get("client_id", ""), form["client_secret"], CLIENT_SECRET_POST, form
        )
    if form.get("client_assertion_type") != ASSERTION_TYPE:
        raise invalid_client("client_assertion_type must be jwt-bearer.")
    return await _by_assertion(assertion, form.get("client_id"), audiences)


def _basic(request: Request) -> tuple[str, str] | None:
    """The client id and secret of ``Authorization: Basic``, if sent.

    Raises:
        OAuthError: ``invalid_client``, for a header that does not read.
    """
    header = request.headers.get("authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise invalid_client("The Basic credentials do not read.", basic=True) from None
    client_id, colon, secret = decoded.partition(":")
    if not colon:
        raise invalid_client("The Basic credentials do not read.", basic=True)
    # RFC 6749 2.3.1: both are form-encoded before they are joined.
    return unquote_plus(client_id), unquote_plus(secret)


async def _by_secret(
    client_id: str, secret: str, method: str, form: Mapping[str, str]
) -> CibaClient:
    """The client whose secret, sent by ``method``, this is.

    Raises:
        OAuthError: ``invalid_client``: unknown, another method, or wrong.
    """
    if form.get("client_id", client_id) != client_id:
        raise OAuthError("invalid_request", "Name one client.")
    async with CibaUnitOfWork() as uow:
        client = await uow.clients.get(client_id) if client_id else None
    basic = method == CLIENT_SECRET_BASIC
    if (
        client is None
        or client.auth_method != method
        or client.secret_digest is None
        or not hmac.compare_digest(client.secret_digest, secret_digest(secret))
    ):
        raise invalid_client("The client could not be authenticated.", basic=basic)
    return client


async def _by_assertion(
    assertion: str, named: str | None, audiences: Collection[str]
) -> CibaClient:
    """The client that signed ``assertion``, its ``jti`` spent now.

    Raises:
        OAuthError: ``invalid_client``, for anything that does not hold.
    """
    import jwt

    refused = invalid_client("The client assertion does not hold.")
    if len(assertion) > ASSERTION_LENGTH:
        raise refused
    try:
        header = jwt.get_unverified_header(assertion)
    except jwt.PyJWTError:
        raise refused from None
    client_id = _named_issuer(assertion)
    if client_id is None or (named is not None and named != client_id):
        raise refused
    async with CibaUnitOfWork() as uow:
        client = await uow.clients.get(client_id)
    if client is None or client.auth_method != PRIVATE_KEY_JWT or not client.jwks:
        raise refused
    claims = _verified(assertion, header, client, audiences)
    if claims is None:
        raise refused
    expires = datetime.fromtimestamp(claims["exp"], UTC)
    async with CibaUnitOfWork() as uow:
        if not await uow.assertions.use(client.client_id, claims["jti"], expires):
            raise refused  # A replay.
        await uow.commit()
    return client


def _named_issuer(assertion: str) -> str | None:
    """The ``iss`` an assertion names, read without trusting it.

    It only says whose keys to verify the assertion with: ``_verified``
    then checks the signature, and that ``iss`` is that client. None: the
    payload does not read, or names no issuer.
    """
    segment = assertion.split(".")[1]  # Its header read: three segments.
    payload = segment + "=" * (-len(segment) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, RecursionError):  # binascii and Unicode errors are too.
        return None
    issuer = claims.get("iss") if isinstance(claims, dict) else None
    return issuer if isinstance(issuer, str) else None


def _verified(
    assertion: str,
    header: Mapping[str, Any],
    client: CibaClient,
    audiences: Collection[str],
) -> dict[str, Any] | None:
    """The assertion's claims, if one of the client's keys signed it as asked."""
    import jwt

    algorithm = header.get("alg")
    if algorithm not in ASSERTION_ALGORITHMS:
        return None
    kid = header.get("kid")
    keys = [
        key
        for key in jwt.PyJWKSet.from_json(client.jwks or "").keys
        if kid is None or key.key_id == kid
    ]
    now = utc_now()
    for key in keys:
        try:
            claims: dict[str, Any] = jwt.decode(
                assertion,
                key.key,
                algorithms=[algorithm],
                audience=list(audiences),
                issuer=client.client_id,
                options={"require": ["exp", "iss", "sub", "aud", "jti"]},
                leeway=SKEW,
            )
        except (jwt.PyJWTError, ValueError, TypeError):
            continue
        jti = claims["jti"]
        latest = now + ciba_settings().ASSERTION_MAX_AGE + SKEW
        if (
            claims["sub"] == client.client_id
            and isinstance(jti, str)
            and 0 < len(jti) <= JTI_LENGTH
            and isinstance(claims["exp"], int | float)
            and datetime.fromtimestamp(claims["exp"], UTC) <= latest
        ):
            return claims
        return None
    return None
