"""The OpenID Provider's CIBA routes, for clients that ask a user's phone.

::

    from domainkit.contrib.ciba.oidc import router as ciba_router
    from domainkit.contrib.ciba.oidc import well_known

    router.include_router(ciba_router, prefix="/oauth")
    router.include_router(well_known)  # At the root: /.well-known/...

- ``POST /bc-authorize``: the backchannel authentication request (CIBA
  Core 7.1): ``scope`` with ``openid``, one of ``login_hint`` or
  ``id_token_hint``, and a ``binding_message``. Answers ``auth_req_id``,
  ``expires_in`` and ``interval``.
- ``POST /qr-authorize``: this framework's extension, the same without a
  hint; answers ``qr`` too, for the client's page to show.
- ``POST /token`` with ``grant_type=urn:openid:params:grant-type:ciba``:
  polls; once approved, ``access_token`` (opaque, limited to the scopes
  granted, ``ACCESS_TOKEN_TTL``), ``id_token``, ``expires_in``, ``scope``.
- ``GET /.well-known/openid-configuration``, ``GET /.well-known/jwks.json``.

The endpoints take form-encoded bodies, authenticate the client first
(``clients``), and answer errors in OAuth's format, never cached. Only the
poll mode is offered. The ID token is signed with ``AUTH.JWT``'s key,
which must be asymmetric, with an ``ISSUER`` (``ciba.E002``).
"""

import base64
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.routing import NoMatchFound

from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.backends import get_all_permissions
from domainkit.contrib.auth.integration import utc_now
from domainkit.contrib.auth.models import UserId
from domainkit.contrib.auth.settings import auth_settings
from domainkit.contrib.auth.throttling import attempt
from domainkit.contrib.auth.uow import AuthUnitOfWork
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import Public
from domainkit.http.errors import APIError

from .clients import ASSERTION_ALGORITHMS, METHODS, authenticate_client
from .exceptions import OAuthError
from .services import Requester, Started, ciba_settings, poll, start_push, start_qr
from .stores import CibaClient, CibaUnitOfWork

__all__ = ["CIBA_GRANT", "id_token_signer", "router", "well_known"]

CIBA_GRANT: Final = "urn:openid:params:grant-type:ciba"
"""The grant type a client polls the token endpoint with (CIBA 10.1)."""
OPENID: Final = "openid"
HINTS: Final = ("login_hint", "id_token_hint", "login_hint_token")
FORM: Final = "application/x-www-form-urlencoded"
BODY_LENGTH: Final = 65536
"""Bytes a form may have: a client assertion and an ID token hint fit."""
BINDING_LENGTH: Final = 64
"""Characters a binding message may have: a short code, read on both devices."""
HINT_LENGTH: Final = 8192
CLIENTS_THROTTLED: Final = "Too many sign-ins were started; try again later."
NO_STORE: Final = {"Cache-Control": "no-store", "Pragma": "no-cache"}
AMR: Final = ("hwk",)
"""How the user authenticated: a key held in hardware, the bound device."""

router = APIRouter(tags=["openid provider"])
well_known = APIRouter(tags=["openid provider"])
"""The provider's metadata and keys, mounted at the root: ``/.well-known/...``."""


def _answer(body: Mapping[str, Any], status_code: int = 200) -> JSONResponse:
    """A JSON answer no cache keeps."""
    return JSONResponse(dict(body), status_code=status_code, headers=NO_STORE)


async def _oauth(work: Callable[[], Awaitable[Mapping[str, Any]]]) -> JSONResponse:
    """Run ``work``; answer what it returns, or its error in OAuth's format."""
    try:
        return _answer(await work())
    except OAuthError as exc:
        error = {"error": exc.error, "error_description": exc.description}
        return JSONResponse(
            error, status_code=exc.status_code, headers={**NO_STORE, **exc.headers}
        )
    except APIError as exc:  # A poll's answer, or a throttle's.
        error = {"error": exc.code, "error_description": str(exc.detail)}
        return JSONResponse(
            error,
            status_code=exc.status_code,
            headers={**NO_STORE, **(exc.headers or {})},
        )


async def _form(request: Request) -> dict[str, str]:
    """The request's form parameters, each sent once (RFC 6749 3.2).

    Raises:
        OAuthError: ``invalid_request``: not a form, too long, or a
            parameter twice.
    """
    if request.headers.get("content-type", "").split(";")[0].strip() != FORM:
        raise OAuthError("invalid_request", f"Send the parameters as {FORM}.")
    body = await request.body()
    if len(body) > BODY_LENGTH:
        raise OAuthError("invalid_request", "The request is too long.")
    try:
        pairs = parse_qsl(body.decode(), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError:
        raise OAuthError("invalid_request", "The request does not read.") from None
    form: dict[str, str] = {}
    for name, value in pairs:
        if name in form:
            raise OAuthError("invalid_request", f"{name} is sent twice.")
        form[name] = value
    return form


def issuer() -> str:
    """``AUTH.JWT.ISSUER``, which an asymmetric key signs for.

    Raises:
        ImproperlyConfigured: No issuer, or a shared-secret algorithm
            (``ciba.E002`` says so at startup).
    """
    config = auth_settings().JWT
    if config.ISSUER is None or config.ALGORITHM.startswith("HS"):
        raise ImproperlyConfigured(
            "The CIBA routes sign ID tokens with AUTH.JWT: set an asymmetric "
            "ALGORITHM, its keys, and an ISSUER."
        )
    return config.ISSUER


def _endpoint(request: Request) -> str:
    """The URL the request was sent to, without its query."""
    return str(request.url.replace(query=""))


async def _client(request: Request, form: Mapping[str, str]) -> CibaClient:
    """The authenticated client, counted against ``CLIENT_RATE``."""
    client = await authenticate_client(
        request, form, audiences={issuer(), _endpoint(request)}
    )
    await attempt(
        request,
        [(f"ciba-client:{client.client_id}", ciba_settings().CLIENT_RATE)],
        action="ciba.client",
        detail=CLIENTS_THROTTLED,
    )
    return client


def _scope(form: Mapping[str, str], client: CibaClient) -> str:
    """The scope asked: ``openid``, and permission keys the client is allowed.

    Raises:
        OAuthError: ``invalid_scope``.
    """
    asked = form.get("scope", "").split()
    if OPENID not in asked:
        raise OAuthError("invalid_scope", "The scope must hold openid.")
    if extra := set(asked) - {OPENID} - client.allowed_scopes:
        raise OAuthError("invalid_scope", f"Not allowed: {' '.join(sorted(extra))}.")
    return " ".join(dict.fromkeys(asked))


def _binding_message(form: Mapping[str, str]) -> str | None:
    """The binding message: short, printable, and there if required.

    Raises:
        OAuthError: ``invalid_binding_message``.
    """
    message = form.get("binding_message")
    if message is None:
        if ciba_settings().REQUIRE_BINDING_MESSAGE:
            raise OAuthError(
                "invalid_binding_message", "Send a binding_message to show."
            )
        return None
    if not 0 < len(message) <= BINDING_LENGTH or not message.isprintable():
        raise OAuthError(
            "invalid_binding_message",
            f"A binding_message is 1 to {BINDING_LENGTH} printable characters.",
        )
    return message


def _check_expiry(form: Mapping[str, str]) -> None:
    """``requested_expiry``, if sent, is a positive integer (it is not honoured).

    Raises:
        OAuthError: ``invalid_request``.
    """
    asked = form.get("requested_expiry")
    if asked is not None and not (asked.isdigit() and int(asked) > 0):
        raise OAuthError("invalid_request", "requested_expiry is a positive integer.")


async def _hinted_user(
    form: Mapping[str, str], client: CibaClient
) -> AbstractBaseUser | None:
    """The user the one hint names; None when nobody known (the request is a decoy).

    Raises:
        OAuthError: ``invalid_request``: no hint, two, an unsupported one,
            or an ID token hint this provider did not sign for the client.
    """
    given = [name for name in HINTS if name in form]
    if len(given) != 1:
        raise OAuthError("invalid_request", "Send exactly one hint of the user.")
    value = form[given[0]]
    if given[0] == "login_hint_token" or not 0 < len(value) <= HINT_LENGTH:
        raise OAuthError("invalid_request", "Send a login_hint or an id_token_hint.")
    async with AuthUnitOfWork() as uow:
        if given[0] == "login_hint":
            return await uow.users.find_by_identifier(value)
        return await uow.users.find(UserId(_hinted_subject(value, client)))


def _hinted_subject(raw: str, client: CibaClient) -> UUID:
    """The ``sub`` of an ID token this provider signed for ``client``.

    Its lifetime is not checked: it names the user, it proves nothing.

    Raises:
        OAuthError: ``invalid_request``: not such a token.
    """
    import jwt

    signer = id_token_signer()
    refused = OAuthError("invalid_request", "The id_token_hint does not hold.")
    try:
        key = signer.keys.get(jwt.get_unverified_header(raw).get("kid", ""))
        if key is None:
            raise refused
        claims = jwt.decode(
            raw,
            key.key,
            algorithms=[key.algorithm],
            audience=client.client_id,
            issuer=issuer(),
            options={"verify_exp": False, "require": ["sub", "iss", "aud"]},
        )
    except jwt.PyJWTError:
        raise refused from None
    try:
        return UUID(claims["sub"])
    except (TypeError, ValueError, AttributeError):
        raise refused from None


def _started(started: Started) -> dict[str, Any]:
    """CIBA's answer to an authentication request."""
    answer: dict[str, Any] = {
        "auth_req_id": started.handle,
        "expires_in": started.expires_in,
        "interval": started.interval,
    }
    if started.qr is not None:
        answer["qr"] = started.qr
    return answer


@router.post(
    "/bc-authorize",
    name="ciba_backchannel_authentication",
    dependencies=[Public],
    response_class=JSONResponse,
)
async def backchannel_authentication(request: Request) -> JSONResponse:
    """Ask the user's phone to approve a sign-in for the client (CIBA 7.1)."""

    async def work() -> Mapping[str, Any]:
        form = await _form(request)
        client = await _client(request, form)
        scope = _scope(form, client)
        message = _binding_message(form)
        _check_expiry(form)
        user = await _hinted_user(form, client)
        async with CibaUnitOfWork() as uow:
            started = await start_push(
                uow,
                user,
                requester=Requester(None, None),
                client_id=client.client_id,
                scope=scope,
                binding_message=message,
            )
            await uow.commit()
        return _started(started)

    return await _oauth(work)


@router.post(
    "/qr-authorize",
    name="ciba_qr_authentication",
    dependencies=[Public],
    response_class=JSONResponse,
)
async def qr_authentication(request: Request) -> JSONResponse:
    """A QR code for the client's page; the phone that scans it answers."""

    async def work() -> Mapping[str, Any]:
        form = await _form(request)
        client = await _client(request, form)
        scope = _scope(form, client)
        message = _binding_message(form)
        if any(name in form for name in HINTS):
            raise OAuthError("invalid_request", "A QR request names no user.")
        async with CibaUnitOfWork() as uow:
            started = await start_qr(
                uow,
                requester=Requester(None, None),
                client_id=client.client_id,
                scope=scope,
                binding_message=message,
            )
            await uow.commit()
        return _started(started)

    return await _oauth(work)


@router.post(
    "/token", name="ciba_token", dependencies=[Public], response_class=JSONResponse
)
async def token(request: Request) -> JSONResponse:
    """Poll a request; once approved, the client's tokens (CIBA 10, 11)."""

    async def work() -> Mapping[str, Any]:
        form = await _form(request)
        client = await _client(request, form)
        if form.get("grant_type") != CIBA_GRANT:
            raise OAuthError("unsupported_grant_type", "Only CIBA's grant is offered.")
        handle = form.get("auth_req_id", "")
        if not 0 < len(handle) <= HINT_LENGTH:
            raise OAuthError("invalid_request", "Send the auth_req_id.")
        approved = await poll(handle, client_id=client.client_id)
        async with AuthUnitOfWork() as uow:
            user = (
                None
                if approved.user_id is None
                else await uow.users.find(UserId(approved.user_id))
            )
            if user is None or not user.is_active:
                raise OAuthError("invalid_grant", "The user cannot sign in.")
            granted = await _granted(approved.scope, client, user)
            config = ciba_settings()
            access = await uow.tokens.issue(
                user.id,
                ttl=config.ACCESS_TOKEN_TTL,
                name=f"ciba:{client.client_id}",
                scopes=granted,
            )
            await uow.commit()
        auth_time = approved.approved_at or utc_now()
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": int(config.ACCESS_TOKEN_TTL.total_seconds()),
            "scope": " ".join([OPENID, *sorted(granted)]),
            "id_token": _id_token(user, client, access, int(auth_time.timestamp())),
        }

    return await _oauth(work)


async def _granted(
    scope: str, client: CibaClient, user: AbstractBaseUser
) -> frozenset[str]:
    """The permission keys asked, still allowed to the client, and the user's."""
    asked = set(scope.split()) - {OPENID}
    return frozenset(asked & client.allowed_scopes & await get_all_permissions(user))


def id_token_signer() -> Any:  # noqa: ANN401 - auth's JWT class, loaded lazily.
    """``AUTH.JWT``'s keys, as ``JWTAuthentication`` loads and checks them.

    Raises:
        ImproperlyConfigured: The key is shared, or there is no issuer.
    """
    from domainkit.contrib.auth.authentication import JWTAuthentication

    issuer()
    return JWTAuthentication()


def _id_token(
    user: AbstractBaseUser, client: CibaClient, access: str, auth_time: int
) -> str:
    """An ID token for ``client``, signed with ``AUTH.JWT``'s key."""
    signer = id_token_signer()
    now = utc_now()
    algorithm: str = signer.config.ALGORITHM
    claims = {
        "iss": issuer(),
        "sub": str(user.id),
        "aud": client.client_id,
        "iat": now,
        "exp": now + ciba_settings().ID_TOKEN_TTL,
        "auth_time": auth_time,
        "amr": list(AMR),
        "at_hash": _at_hash(access, algorithm),
    }
    encoded: str = signer.jwt.encode(
        claims,
        signer.signing_key,
        algorithm=algorithm,
        headers={"kid": signer.key_id},
    )
    return encoded


def _at_hash(access: str, algorithm: str) -> str:
    """The left half of the access token's hash, in base64url (OIDC 3.2.2.9)."""
    bits = "512" if algorithm == "EdDSA" else algorithm[-3:]
    digest = hashlib.new(f"sha{bits}", access.encode()).digest()
    return base64.urlsafe_b64encode(digest[: len(digest) // 2]).rstrip(b"=").decode()


def _url(request: Request, name: str) -> str | None:
    """The absolute URL of the route ``name``, if the application mounts it."""
    try:
        return str(request.url_for(name))
    except NoMatchFound:
        return None


@well_known.get(
    "/.well-known/openid-configuration", dependencies=[Public], name="ciba_metadata"
)
async def metadata(request: Request) -> dict[str, Any]:
    """The provider's metadata (OIDC Discovery), with CIBA's (CIBA 4)."""
    signer = id_token_signer()
    answer: dict[str, Any] = {
        "issuer": issuer(),
        "token_endpoint": _url(request, "ciba_token"),
        "backchannel_authentication_endpoint": _url(
            request, "ciba_backchannel_authentication"
        ),
        "jwks_uri": _url(request, "ciba_jwks"),
        "grant_types_supported": [CIBA_GRANT],
        "backchannel_token_delivery_modes_supported": ["poll"],
        "backchannel_user_code_parameter_supported": False,
        "token_endpoint_auth_methods_supported": list(METHODS),
        "token_endpoint_auth_signing_alg_values_supported": list(ASSERTION_ALGORITHMS),
        "id_token_signing_alg_values_supported": [signer.config.ALGORITHM],
        "subject_types_supported": ["public"],
        "scopes_supported": [OPENID],
        "claims_supported": ["iss", "sub", "aud", "iat", "exp", "auth_time", "amr"],
    }
    return {key: value for key, value in answer.items() if value is not None}


@well_known.get("/.well-known/jwks.json", dependencies=[Public], name="ciba_jwks")
async def jwks() -> dict[str, Any]:
    """The public keys ID tokens are signed with: the current, and the retiring."""
    from jwt.algorithms import get_default_algorithms

    signer = id_token_signer()
    algorithms = get_default_algorithms()
    keys = []
    for key_id, key in signer.keys.items():
        jwk = algorithms[key.algorithm].to_jwk(key.key, as_dict=True)
        keys.append({**jwk, "kid": key_id, "alg": key.algorithm, "use": "sig"})
    return {"keys": keys}
