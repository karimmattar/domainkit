"""Browser sessions: an opaque token in an HttpOnly cookie, guarded against CSRF.

For browser apps, which should keep tokens where scripts cannot read them.
List the class after the classes that read headers, and mount the router::

    AUTH = {
        "AUTHENTICATION_CLASSES": [
            "domainkit.contrib.auth.authentication.TokenAuthentication",
            "domainkit.contrib.auth.cookies.CookieAuthentication",
        ],
        "CSRF_TRUSTED_ORIGINS": ["https://app.example.com"],
    }

    from domainkit.contrib.auth.cookies import router as session_router

    router.include_router(session_router, prefix="/auth")

``POST /session`` logs in, sets the session cookie (``AUTH.COOKIE.NAME``:
HttpOnly, Secure unless ``DEBUG``, ``SameSite``) and the CSRF cookie
(readable by scripts), and returns the CSRF token; with a second factor
it answers 202 with a challenge, and ``POST /session/mfa`` with a code
finishes. ``GET /session`` returns the token again, and ``DELETE /session``
logs out and clears both.

A request the session cookie authenticates, with a method other than
``GET``, ``HEAD``, or ``OPTIONS``, must come from a trusted origin (its
``Origin``, or ``Referer`` without one, is its own origin, a host of
``ALLOWED_HOSTS`` with the request's scheme, or in
``AUTH.CSRF_TRUSTED_ORIGINS``; an HTTPS request with neither is refused),
and must send the CSRF token in
``X-CSRF-Token``, equal to the cookie's and signed for this session.
Otherwise it gets ``403 csrf_failed``. A WebSocket handshake with the
cookie must come from a trusted origin. Requests with an ``Authorization``
header are left to the classes that read it, and a session that ended
leaves the request anonymous, so a stale cookie never stops a login.
"""

import secrets
from typing import Final
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from domainkit.conf import settings
from domainkit.http.errors import APIError
from domainkit.http.middleware import host_allowed
from domainkit.signing import BadSignature, Signer, constant_time_compare

from .access import SAFE_METHODS, Public
from .api import (
    LoginIn,
    MfaChallengeOut,
    _acting_as,
    _credentials,
    _errors,
    _no_store,
    check_password,
)
from .authentication import AuthenticationFailed, BaseAuthentication, Credentials
from .events import UserLoggedOut
from .mfa import MfaLoginIn, challenge, verify_challenge
from .models import AbstractBaseUser, UserId
from .settings import auth_settings
from .tokens import TokenKind, token_digest, well_formed
from .uow import AuthUnitOfWork

SCHEME: Final = "cookie"
"""``request.auth.scheme`` of requests a session cookie authenticates."""
WEB_SCHEMES: Final = {"ws": "http", "wss": "https"}

router = APIRouter(tags=["auth"])


class CSRFFailed(AuthenticationFailed):
    """A cookie request came from an untrusted origin, or without its token (403)."""

    def __init__(self, detail: str) -> None:
        """Use status 403 and the code ``csrf_failed``."""
        super().__init__(
            detail, status_code=status.HTTP_403_FORBIDDEN, code="csrf_failed"
        )


class CookieAuthentication(BaseAuthentication):
    """Browser sessions: the session cookie, checked against CSRF.

    Sessions start at ``POST /session`` (``router``); this class issues no
    tokens for ``/login``, so list another class first.
    """

    async def authenticate(self, connection: HTTPConnection) -> Credentials | None:
        """The user of a live session cookie, if the request may use it.

        Raises:
            CSRFFailed: The request does not pass the CSRF checks.
        """
        if "authorization" in connection.headers:
            return None
        raw = connection.cookies.get(auth_settings().COOKIE.NAME)
        if raw is None or not well_formed(raw, auth_settings().TOKEN_PREFIX):
            return None
        async with AuthUnitOfWork() as uow:
            token = await uow.tokens.find(raw)
            if token is None or token.kind != TokenKind.SESSION or token.expired():
                return None
            user = await uow.users.find(UserId(token.user_id))
        if user is None or not user.is_active:
            return None
        check_request(connection, token.id)
        return Credentials(user, scheme=SCHEME, token_id=token.id)


def check_request(connection: HTTPConnection, session: str) -> None:
    """Refuse a cookie request that may come from another site.

    Raises:
        CSRFFailed: An unsafe method without a trusted origin and the token,
            or a WebSocket handshake from an untrusted origin.
    """
    if connection.scope["type"] == "websocket":
        check_origin(connection, required=True)
    elif connection.scope["method"] not in SAFE_METHODS:
        check_origin(connection)
        check_token(connection, session)


def check_origin(connection: HTTPConnection, *, required: bool = False) -> None:
    """Refuse a request whose ``Origin`` (or ``Referer``) is not trusted.

    Without either, HTTPS requests (and ``required`` ones) are refused, as
    browsers send one there; plain HTTP ones, in development, pass.

    Raises:
        CSRFFailed: The origin is missing where it must be, or not trusted.
    """
    origin = connection.headers.get("origin")
    if origin is None and (referer := connection.headers.get("referer")):
        parts = urlsplit(referer)
        origin = f"{parts.scheme}://{parts.netloc}"
    if origin is None:
        if required or connection.url.scheme in {"https", "wss"}:
            raise CSRFFailed("The request names no origin (Origin or Referer).")
        return
    if not trusted_origin(origin, connection):
        raise CSRFFailed("The request comes from an origin that is not trusted.")


def trusted_origin(origin: str, connection: HTTPConnection) -> bool:
    """Whether pages of ``origin`` may send cookie requests here.

    It must be the request's own origin, be in ``AUTH.CSRF_TRUSTED_ORIGINS``
    (``*.`` matching subdomains), or have the request's scheme and a host of
    ``ALLOWED_HOSTS`` (a wildcard ``*`` counts for none).
    """
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False
    for trusted in auth_settings().CSRF_TRUSTED_ORIGINS:
        scheme, _, host = trusted.partition("://")
        if scheme != parts.scheme:
            continue
        if host.startswith("*.") and parts.netloc.endswith(host[1:]):
            return True
        if host == parts.netloc:
            return True
    scheme = WEB_SCHEMES.get(connection.url.scheme, connection.url.scheme)
    if parts.scheme != scheme:
        return False
    if parts.netloc == connection.headers.get("host", "").lower():
        return True
    hosts = [host for host in settings.ALLOWED_HOSTS if host != "*"]
    return host_allowed(parts.netloc, hosts)


def check_token(connection: HTTPConnection, session: str) -> None:
    """Refuse a request without the CSRF token of ``session``.

    The header must equal the cookie, and carry this session's signature.

    Raises:
        CSRFFailed: It does not.
    """
    config = auth_settings().COOKIE
    sent = connection.headers.get(config.CSRF_HEADER)
    cookie = connection.cookies.get(config.CSRF_NAME)
    if sent and cookie is not None and constant_time_compare(sent, cookie):
        try:
            _csrf_signer(session).unsign(sent)
        except BadSignature:
            pass
        else:
            return
    raise CSRFFailed("The CSRF token is missing or wrong.")


def csrf_token(session: str) -> str:
    """A new CSRF token for the session whose token digest is ``session``."""
    return _csrf_signer(session).sign(secrets.token_urlsafe(18))


class CsrfOut(BaseModel):
    """The token to send back in ``X-CSRF-Token`` with unsafe methods."""

    csrf_token: str


@router.post(
    "/session",
    dependencies=[Public],
    responses=_errors(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def log_in(
    body: LoginIn, request: Request, response: Response
) -> CsrfOut | MfaChallengeOut:
    """Check a username and password; start a browser session in cookies.

    A user with a second factor gets 202 and a challenge for
    ``/session/mfa`` instead.
    """
    _no_store(response)
    check_origin(request)
    user = await check_password(request, body)
    if (pending := await challenge(user)) is not None:
        response.status_code = status.HTTP_202_ACCEPTED
        return pending
    return await start_session(user, response)


@router.post(
    "/session/mfa",
    dependencies=[Public],
    responses=_errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def log_in_with_code(
    body: MfaLoginIn, request: Request, response: Response
) -> CsrfOut:
    """Finish a login with a second factor; start the browser session."""
    _no_store(response)
    check_origin(request)
    user = await verify_challenge(request, body.mfa_token, body.code)
    return await start_session(user, response)


@router.get(
    "/session", dependencies=[Public], responses=_errors(status.HTTP_400_BAD_REQUEST)
)
async def session_token(request: Request, response: Response) -> CsrfOut:
    """A new CSRF token for this browser session (also set in its cookie)."""
    _no_store(response)
    credentials = request.scope.get("auth")
    if not isinstance(credentials, Credentials) or credentials.scheme != SCHEME:
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            "This request has no browser session.",
            code="no_session",
        )
    token = csrf_token(str(credentials.token_id))
    _set_csrf_cookie(response, token)
    return CsrfOut(csrf_token=token)


@router.delete(
    "/session",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Public],
    responses=_errors(status.HTTP_403_FORBIDDEN),
)
async def log_out(request: Request, response: Response) -> None:
    """End this browser session, if it is live, and clear its cookies."""
    credentials = request.scope.get("auth")
    if isinstance(credentials, Credentials) and credentials.scheme == SCHEME:
        credentials = _credentials(request)
        async with AuthUnitOfWork() as uow:
            stored = await uow.users.get(UserId(credentials.user.id))
            await uow.tokens.revoke(str(credentials.token_id))
            stored.record(UserLoggedOut(user_id=stored.id, everywhere=False))
            await uow.commit()
    config = auth_settings().COOKIE
    for name, http_only in ((config.NAME, True), (config.CSRF_NAME, False)):
        response.delete_cookie(
            name,
            path=config.PATH,
            domain=config.DOMAIN,
            secure=_secure(),
            httponly=http_only,
            samesite=config.SAMESITE,
        )


async def start_session(user: AbstractBaseUser, response: Response) -> CsrfOut:
    """Record the login, store a session token, and set both cookies."""
    config = auth_settings().COOKIE
    with _acting_as(user):
        async with AuthUnitOfWork() as uow:
            stored = await uow.users.get(UserId(user.id))
            raw = await uow.tokens.issue(
                stored.id, kind=TokenKind.SESSION, ttl=config.TTL, name="browser"
            )
            await uow.users.record_login(stored)
            await uow.commit()
    token = csrf_token(token_digest(raw))
    response.set_cookie(
        config.NAME,
        raw,
        max_age=int(config.TTL.total_seconds()),
        path=config.PATH,
        domain=config.DOMAIN,
        secure=_secure(),
        httponly=True,
        samesite=config.SAMESITE,
    )
    _set_csrf_cookie(response, token)
    return CsrfOut(csrf_token=token)


def _set_csrf_cookie(response: Response, token: str) -> None:
    """Hand scripts the CSRF token: same scope, readable, for the session's life."""
    config = auth_settings().COOKIE
    response.set_cookie(
        config.CSRF_NAME,
        token,
        max_age=int(config.TTL.total_seconds()),
        path=config.PATH,
        domain=config.DOMAIN,
        secure=_secure(),
        httponly=False,
        samesite=config.SAMESITE,
    )


def _secure() -> bool:
    """``AUTH.COOKIE.SECURE``, or HTTPS-only unless ``DEBUG``."""
    secure = auth_settings().COOKIE.SECURE
    return not settings.DEBUG if secure is None else secure


def _csrf_signer(session: str) -> Signer:
    """Signs CSRF tokens for one session alone."""
    return Signer(salt=f"domainkit.auth.csrf:{session}")
