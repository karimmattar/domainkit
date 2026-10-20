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
  gets 409 rather than the token again.
- ``DELETE /tokens/{id}`` revokes one of the user's tokens (a JWT login's
  refresh token takes its login with it).

Only credentials without scopes may use these routes (``IsUnscoped``): a
scoped token can neither see the others nor make a token with more. A
token never carries a scope its user lacks when it is made; scopes only
narrow what the user may do, so a permission taken from the user later is
gone from the token too. Tokens are identified by the digest the table
keeps, never by their value.
"""

from datetime import datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.http.errors import APIError
from domainkit.http.pagination import (
    CURSOR_PATTERN,
    MAX_CURSOR_LENGTH,
    InvalidCursor,
    decode_cursor,
    encode_cursor,
)

from .access import IsAuthenticated, IsUnscoped, Require
from .api import _credentials, _errors, _no_store
from .backends import get_all_permissions
from .events import TokenCreated, TokenRevoked
from .models import UserId
from .tokens import AuthToken, TokenKind
from .uow import AuthUnitOfWork

PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 100
MAX_SCOPES: Final = 100
NAME_MAX_LENGTH: Final = 100
DIGEST_PATTERN: Final = r"^[0-9a-f]{64}$"

router = APIRouter(
    tags=["auth"], dependencies=[Depends(Require(IsAuthenticated(), IsUnscoped()))]
)


class TokenIn(BaseModel):
    """A personal token to make."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, max_length=NAME_MAX_LENGTH, pattern=r"^[^\x00-\x1f\x7f]+$"
    )
    """What it is for, as the list shows it: one line."""
    scopes: list[Annotated[str, Field(min_length=1, max_length=255)]] | None = Field(
        default=None, max_length=MAX_SCOPES
    )
    """Permission keys it is limited to; None: all of the user's."""
    expires_in_days: int | None = Field(default=90, ge=1, le=3650)
    """Days it works; None: until revoked."""


class TokenInfo(BaseModel):
    """One of the user's tokens, without its value."""

    id: str
    """Its SHA-256 digest: what revoking it takes."""
    name: str
    kind: str
    """``access``, ``session``, or ``refresh``."""
    scopes: list[str] | None
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    """When it last authenticated a request, to five minutes."""
    current: bool
    """Whether this request uses it."""


class NewToken(TokenInfo):
    """A token just made: its value is shown now, and never again."""

    token: str


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
    """The user's tokens, newest first."""
    credentials = _credentials(request)
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
    responses=_errors(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
)
async def create_token(body: TokenIn, request: Request, response: Response) -> NewToken:
    """Make a personal token, limited to ``scopes`` if given; it is shown once."""
    _no_store(response)
    user = _credentials(request).user
    scopes = None if body.scopes is None else frozenset(body.scopes)
    if scopes is not None and (
        missing := sorted(scopes - await get_all_permissions(user))
    ):
        raise APIError(
            status.HTTP_403_FORBIDDEN,
            f"A token cannot have permissions you lack: {', '.join(missing)}.",
            code="scope_not_held",
        )
    ttl = None if body.expires_in_days is None else timedelta(days=body.expires_in_days)
    async with AuthUnitOfWork() as uow:
        stored = await uow.users.get(UserId(user.id))
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
    """Revoke one of the user's tokens; a refresh token ends its login."""
    user = _credentials(request).user
    async with AuthUnitOfWork() as uow:
        token = await uow.tokens.find_digest(token_id)
        if token is None or token.user_id != user.id:
            raise APIError(
                status.HTTP_404_NOT_FOUND, "No such token.", code="token_not_found"
            )
        if token.kind == TokenKind.REFRESH and token.family is not None:
            await uow.tokens.revoke_family(token.family)
        else:
            await uow.tokens.revoke(token.id)
        stored = await uow.users.get(UserId(user.id))
        stored.record(TokenRevoked(user_id=user.id, name=token.name))
        await uow.commit()


def _info(token: AuthToken, request: Request) -> TokenInfo:
    """What the list shows of ``token``."""
    credentials = _credentials(request)
    current = token.id == credentials.token_id or (
        token.family is not None and token.family == credentials.session
    )
    return TokenInfo(
        id=token.id,
        name=token.name,
        kind=token.kind,
        scopes=None if token.scopes is None else sorted(token.scopes),
        created_at=token.created_at,
        expires_at=token.expires_at,
        last_used_at=token.last_used_at,
        current=current,
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
