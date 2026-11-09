"""Personal tokens: users list, create, and revoke their own.

Mount the router next to the one that logs in::

    from domainkit.contrib.auth.personal_tokens import router as tokens_router

    router.include_router(tokens_router, prefix="/auth")

- ``GET /tokens`` lists the user's tokens, newest first, by pages: logins,
  browser sessions, and personal tokens, each with when it was last used.
- ``POST /tokens`` makes an opaque token for a script, limited to
  ``scopes`` (permission keys the user has) if given, and shows it once:
  the answer is marked ``Cache-Control: no-store``, so neither caches nor
  ``IdempotencyMiddleware`` keep it, and a retry with its idempotency key
  gets 409 rather than the token again. The user authenticates again
  first: a step-up within 5 minutes where they have a second factor
  (``401 step_up_required`` otherwise), the ``password`` where they do not.
  A token works ``AUTH.PERSONAL_TOKEN_MAX_AGE`` at most (90 days by
  default); one that never ends only where that setting is None (AUTH-16).
- ``DELETE /tokens/{id}`` revokes one of the user's tokens (a JWT login's
  refresh token takes its login with it).

Only credentials without scopes may use these routes (``IsUnscoped``): a
scoped token can neither see the others nor make a token with more. A
personal token is no login: logging out of a session, and idling out, leave
it; a password change, a reset, or logging out everywhere ends it. A
token never carries a scope its user lacks when it is made; scopes only
narrow what the user may do, so a permission taken from the user later is
gone from the token too. Tokens are identified by the digest the table
keeps, never by their value.
"""

from datetime import datetime, timedelta
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from domainkit.http.access import IsAuthenticated, IsUnscoped, Require
from domainkit.http.errors import APIError
from domainkit.http.pagination import (
    CURSOR_PATTERN,
    MAX_CURSOR_LENGTH,
    InvalidCursor,
    decode_cursor,
    encode_cursor,
)

from .access import StepUp
from .api import PASSWORD_MAX_LENGTH, _credentials, _errors, _no_store
from .authentication import AuthenticationFailed, Credentials
from .backends import get_all_permissions
from .events import TokenCreated, TokenRevoked
from .gates import check_manage_logins
from .models import AbstractBaseUser, UserId
from .settings import auth_settings
from .stepup import check_password_again
from .throttling import audit_refusal
from .tokens import LOGIN_NAME, AuthToken, TokenKind
from .uow import AuthUnitOfWork

__all__ = [
    "router",
]

PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 100
MAX_SCOPES: Final = 100
NAME_MAX_LENGTH: Final = 100
DIGEST_PATTERN: Final = r"^[0-9a-f]{64}$"
DEFAULT_DAYS: Final = 90
"""Days a token works when the request names none (within the setting's)."""
STEP_UP_AGE: Final = timedelta(minutes=5)
"""How recent a step-up must be to make a token, for users with a second factor."""
ACTION: Final = "auth.token_create"
"""The audit trail's action for refused attempts to make a token."""

router = APIRouter(
    tags=["auth"], dependencies=[Depends(Require(IsAuthenticated(), IsUnscoped()))]
)


class TokenIn(BaseModel):
    """A personal token to make."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, max_length=NAME_MAX_LENGTH, pattern=r"^[^\x00-\x1f\x7f]+$"
    )
    """What it is for, as the list shows it: one line; never ``login``, the name
    of logins' tokens."""
    scopes: list[Annotated[str, Field(min_length=1, max_length=255)]] | None = Field(
        default=None, max_length=MAX_SCOPES
    )
    """Permission keys it is limited to; None: all of the user's."""
    expires_in_days: int | None = Field(default=DEFAULT_DAYS, ge=1, le=3650)
    """Days it works; None: until revoked, where ``PERSONAL_TOKEN_MAX_AGE``
    allows. Left out: 90, or the setting's, if shorter."""
    password: str | None = Field(
        default=None, min_length=1, max_length=PASSWORD_MAX_LENGTH, repr=False
    )
    """The user's password: needed by users without a second factor, who
    cannot step up."""

    @field_validator("name")
    @classmethod
    def _not_a_login(cls, value: str) -> str:
        """Refuse ``login``: tokens so named are logins' (``tokens.is_login``)."""
        if value == LOGIN_NAME:
            raise ValueError(f"{LOGIN_NAME!r} names logins' tokens; choose another")
        return value


class TokenInfo(BaseModel):
    """One of the user's tokens, without its value."""

    id: str
    """Its SHA-256 digest: what revoking it takes."""
    name: str
    kind: str
    """``access``, ``session``, ``refresh``, or ``certificate``."""
    scopes: list[str] | None
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    """When it last authenticated a request, to five minutes."""
    current: bool
    """Whether this request uses it."""
    method: str | None = None
    """How its login signed in (``LoginMethod``): ``password``, ``passkey``,
    ``device``, ``push``, ``qr``, ``provider``, or ``impersonation``; null
    for personal tokens, and logins made before 2.2."""
    agent: str | None = None
    """The ``User-Agent`` its login was made from, at most 200 characters."""
    ip: str | None = None
    """The address its login was made from."""
    device_id: UUID | None = None
    """The bound device that made its login: the phone's own login."""


class NewToken(TokenInfo):
    """A token just made: its value is shown now, and never again."""

    token: str = Field(repr=False)


class TokenPage(BaseModel):
    """Tokens, and the cursor of the next page."""

    items: list[TokenInfo]
    next_cursor: str | None


@router.get("/tokens", responses=_errors(status.HTTP_401_UNAUTHORIZED))
async def list_tokens(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = PAGE_SIZE,
    cursor: Annotated[
        str | None, Query(pattern=CURSOR_PATTERN, max_length=MAX_CURSOR_LENGTH)
    ] = None,
) -> TokenPage:
    """The user's tokens, newest first, and where each login came from.

    Checked by ``on_manage_logins`` first (ADR 0130).
    """
    credentials = _credentials(request)
    await check_manage_logins(request, credentials.user)
    before = None
    if cursor is not None:
        created, token_id = decode_cursor(cursor, 2)
        before = (_moment(created), token_id)
    async with AuthUnitOfWork() as uow:
        page = await uow.tokens.of_user(credentials.user.id, limit=limit, before=before)
    last = page.items[-1] if page.has_more else None
    return TokenPage(
        items=[_info(token, request) for token in page.items],
        next_cursor=None
        if last is None
        else encode_cursor([last.created_at.isoformat(), last.id]),
    )


@router.post(
    "/tokens",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def create_token(body: TokenIn, request: Request, response: Response) -> NewToken:
    """Make a personal token, limited to ``scopes`` if given; it is shown once.

    The user authenticates again first (``reauthenticate``), and the token
    works ``AUTH.PERSONAL_TOKEN_MAX_AGE`` at most.
    """
    _no_store(response)
    user = _credentials(request).user
    ttl = lifetime(body)
    scopes = None if body.scopes is None else frozenset(body.scopes)
    if scopes is not None and (
        missing := sorted(scopes - await get_all_permissions(user))
    ):
        raise APIError(
            status.HTTP_403_FORBIDDEN,
            f"A token cannot have permissions you lack: {', '.join(missing)}.",
            code="scope_not_held",
        )
    await reauthenticate(request, user, body.password)
    limit = auth_settings().MAX_LIVE_TOKENS
    async with AuthUnitOfWork() as uow:
        await held_credentials(uow, _credentials(request))
        stored = await uow.users.get(UserId(user.id))
        if await uow.tokens.live_count(user.id) >= limit:
            raise APIError(
                status.HTTP_409_CONFLICT,
                f"You hold {limit} live tokens, the most allowed; revoke one first.",
                code="too_many_tokens",
            )
        raw, token = await uow.tokens.create(
            user.id, ttl=ttl, name=body.name, scopes=scopes
        )
        stored.record(
            TokenCreated(
                user_id=user.id,
                name=body.name,
                scopes=None if scopes is None else tuple(sorted(scopes)),
            )
        )
        await uow.commit()
    return NewToken(token=raw, **_info(token, request).model_dump())


@router.delete(
    "/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_errors(status.HTTP_401_UNAUTHORIZED, status.HTTP_404_NOT_FOUND),
)
async def revoke_token(
    token_id: Annotated[str, Path(pattern=DIGEST_PATTERN)], request: Request
) -> None:
    """Revoke one of the user's tokens; a refresh token ends its login.

    Ending another login than the request's own is checked by
    ``on_manage_logins`` first (ADR 0130).
    """
    credentials = _credentials(request)
    user = credentials.user
    async with AuthUnitOfWork() as uow:
        token = await uow.tokens.find_digest(token_id)
        if token is None or token.user_id != user.id:
            raise APIError(
                status.HTTP_404_NOT_FOUND, "No such token.", code="token_not_found"
            )
        if not _own(token, credentials):
            await check_manage_logins(request, user)
        if token.kind == TokenKind.REFRESH and token.family is not None:
            await uow.tokens.revoke_family(token.family)
        else:
            await uow.tokens.revoke(token.id)
        stored = await uow.users.get(UserId(user.id))
        stored.record(TokenRevoked(user_id=user.id, name=token.name))
        await uow.commit()


async def held_credentials(uow: AuthUnitOfWork, credentials: Credentials) -> None:
    """Hold the user's credentials lock, and check the request's still live.

    The password or step-up was checked before, in units of work of their
    own: a password change, or a second factor turned on, may have ended
    these credentials since. Under the lock those hold, a token made here
    never outlives them (SEC-16a-6).

    Raises:
        AuthenticationFailed: The credentials ended meanwhile.
    """
    await uow.tokens.lock_user(credentials.user.id)
    if credentials.token_id is not None:
        live = await uow.tokens.stored(credentials.token_id)
    elif credentials.login is not None:
        live = await uow.tokens.family_active(credentials.login)
    else:
        live = True  # Credentials no token row holds: a gateway's header.
    if not live:
        raise AuthenticationFailed("Session ended; log in again.")


def lifetime(body: TokenIn) -> timedelta | None:
    """How long the token asked for works; None: until revoked.

    Raises:
        APIError: 422 ``token_lifetime_too_long``, past
            ``AUTH.PERSONAL_TOKEN_MAX_AGE``.
    """
    longest = auth_settings().PERSONAL_TOKEN_MAX_AGE
    if "expires_in_days" not in body.model_fields_set:
        asked = timedelta(days=DEFAULT_DAYS)
        return asked if longest is None else min(asked, longest)
    days = body.expires_in_days
    if longest is None:
        return None if days is None else timedelta(days=days)
    if days is None or timedelta(days=days) > longest:
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"A token works {longest.days} days at most here; ask for fewer.",
            code="token_lifetime_too_long",
        )
    return timedelta(days=days)


async def reauthenticate(
    request: Request, user: AbstractBaseUser, password: str | None
) -> None:
    """Make the user prove it is them again before a token is made (AUTH-16).

    With a second factor: a step-up of these credentials within
    ``STEP_UP_AGE`` (``POST /auth/step-up``). Without one: the password,
    counted with the user's other password checks.

    Raises:
        StepUpRequired: 401, no recent step-up.
        APIError: 400 ``password_required`` or ``wrong_password``; 429 past
            ``AUTH.LOGIN_RATE``.
    """
    async with AuthUnitOfWork() as uow:
        factor = await uow.mfa.enabled(user.id)
    if not factor:
        await check_password_again(request, user, password, ACTION)
        return
    rule = StepUp(max_age=STEP_UP_AGE)
    if not await rule.has_permission(request, user):
        await audit_refusal(ACTION, "step_up_required")
        raise await rule.refusal(request, user)


def _own(token: AuthToken, credentials: Credentials) -> bool:
    """Whether ``token`` is the login ``credentials`` authenticate with."""
    return token.id == credentials.token_id or (
        token.family is not None and token.family == credentials.login
    )


def _info(token: AuthToken, request: Request) -> TokenInfo:
    """What the list shows of ``token``."""
    return TokenInfo(
        id=token.id,
        name=token.name,
        kind=token.kind,
        scopes=None if token.scopes is None else sorted(token.scopes),
        created_at=token.created_at,
        expires_at=token.expires_at,
        last_used_at=token.last_used_at,
        current=_own(token, _credentials(request)),
        method=token.method,
        agent=token.agent,
        ip=token.ip,
        device_id=token.device_id,
    )


def _moment(text: str) -> datetime:
    """A cursor's time.

    Raises:
        InvalidCursor: It is not one.
    """
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise InvalidCursor from None
