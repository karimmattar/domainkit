"""Tokens: logins, refresh tokens and personal tokens, by their digest."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Select, delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql.elements import ColumnElement

from domainkit.context import current_request
from domainkit.ddd import Page

from ..principals import (
    HANDLED,
    family_target,
    invalidate_after_commit,
    token_target,
    user_target,
)
from ..settings import auth_settings
from ..tables import tokens
from ..tokens import (
    AGENT_MAX_LENGTH,
    LOGIN_DEVICE,
    LOGIN_NAME,
    AuthToken,
    TokenKind,
    is_login,
    last_used_every,
    login_origin,
    new_token,
    token_digest,
    utc_now,
)
from ..users import get_user_table
from ._shared import changed_rows

CREDENTIALS_LOCK: Final = "domainkit.auth.credentials:{user_id}"
"""The lock every change to a user's credentials holds on SQLite (SEC-16a-6)."""


class TokenStore:
    """Opaque tokens, found by their digest; the raw token is never stored.

    What ends a user's tokens (``revoke_user``, ``revoke_family``) and what
    issues one on a check made earlier (a login, a refresh, a personal
    token) hold the user's credentials lock (``lock_user``) until they
    commit, so a token issued meanwhile is never left behind: an issue
    that waits sees the end and refuses, and an end that waits deletes
    what was issued (SEC-16a-6).
    """

    def __init__(
        self,
        session: AsyncSession,
        lock: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Work in ``session``; ``lock`` is the unit of work's (``uow.lock``)."""
        self.session = session
        self._lock = lock

    async def lock_user(self, user_id: UUID) -> None:
        """Hold the user's credentials lock until the transaction ends.

        On PostgreSQL, the user's row (``FOR UPDATE``): what changes the
        user's password, or records an event of theirs, writes that row, so
        every transaction about the user's credentials waits on one lock,
        and none waits on two in another order. On SQLite, the database's
        write lock (``uow.lock``). Without the unit of work's lock (a store
        on a bare session), none.
        """
        if self._lock is None:
            return
        if self.session.get_bind().dialect.name == "postgresql":
            table = get_user_table()
            await self.session.execute(
                select(table.c.id).where(table.c.id == user_id).with_for_update()
            )
            return
        await self._lock(CREDENTIALS_LOCK.format(user_id=user_id))

    async def stored(self, digest: str) -> bool:
        """Whether the token ``digest`` is still stored, expired or not."""
        statement = select(exists().where(tokens.c.id == digest))
        return bool(await self.session.scalar(statement))

    async def issue(
        self,
        user_id: UUID,
        *,
        kind: TokenKind = TokenKind.ACCESS,
        ttl: timedelta | None,
        name: str = "",
        family: UUID | None = None,
        now: datetime | None = None,
        scopes: frozenset[str] | None = None,
        like: AuthToken | None = None,
    ) -> str:
        """Store a new token for ``user_id`` and return it (shown only now).

        It works for ``ttl`` from ``now`` (default: the current time), with
        the permission keys in ``scopes`` only, if given. ``like``: a token
        whose login's origin it keeps (``create``).
        """
        raw, _ = await self.create(
            user_id,
            kind=kind,
            ttl=ttl,
            name=name,
            family=family,
            now=now,
            scopes=scopes,
            like=like,
        )
        return raw

    async def create(
        self,
        user_id: UUID,
        *,
        kind: TokenKind = TokenKind.ACCESS,
        ttl: timedelta | None,
        name: str = "",
        family: UUID | None = None,
        now: datetime | None = None,
        scopes: frozenset[str] | None = None,
        like: AuthToken | None = None,
    ) -> tuple[str, AuthToken]:
        """``issue``, returning the stored token with the raw one.

        A login's token records where the login came from (ADR 0130): how
        it signed in and its device (``logging_in``), and the request's
        address and agent. ``like`` gives a token whose origin it keeps
        instead, as a refreshed login does.
        """
        raw = new_token(auth_settings().TOKEN_PREFIX)
        now = now or utc_now()
        token = AuthToken(
            id=token_digest(raw),
            user_id=user_id,
            kind=kind,
            name=name,
            family=family,
            created_at=now,
            expires_at=None if ttl is None else now + ttl,
            scopes=scopes,
            **_origin_of(kind, name, like),
        )
        self.session.add(token)
        return raw, token

    async def add_certificate(self, user_id: UUID, digest: str, *, name: str) -> None:
        """Let the certificate whose SHA-256 is ``digest`` authenticate ``user_id``."""
        self.session.add(
            AuthToken(
                id=digest,
                user_id=user_id,
                kind=TokenKind.CERTIFICATE,
                name=name,
                created_at=utc_now(),
            )
        )

    async def find(self, raw: str) -> AuthToken | None:
        """The stored token ``raw`` is, expired or not."""
        return await self.find_digest(token_digest(raw))

    async def find_digest(self, digest: str) -> AuthToken | None:
        """The stored token whose digest is ``digest``."""
        return await self.session.get(AuthToken, digest)

    async def touch(self, token: AuthToken, now: datetime | None = None) -> bool:
        """Record that ``token`` was used, at most once per ``last_used_every()``.

        Concurrent requests write once: the update checks the stored time.
        """
        moment = now or utc_now()
        last = token.last_used_at
        if last is not None and moment - last < last_used_every():
            return False
        written = await self.touch_digest(token.id, moment)
        set_committed_value(token, "last_used_at", moment)
        return written

    async def touch_digest(self, digest: str, now: datetime) -> bool:
        """Write ``now`` as the ``last_used_at`` of token ``digest``, if it is due.

        Due when none is stored, or the stored one is ``last_used_every()``
        old: of concurrent writers, from any process, one wins.
        """
        result = await self.session.execute(
            update(tokens)
            .where(
                tokens.c.id == digest,
                tokens.c.last_used_at.is_(None)
                | (tokens.c.last_used_at <= now - last_used_every()),
            )
            .values(last_used_at=now)
            .execution_options(**HANDLED)  # A last use changes no principal.
        )
        return changed_rows(result) == 1

    async def live_count(
        self,
        user_id: UUID,
        kind: TokenKind = TokenKind.ACCESS,
        now: datetime | None = None,
    ) -> int:
        """How many of ``user_id``'s tokens of ``kind`` have not expired."""
        moment = now or utc_now()
        statement = select(func.count()).where(
            tokens.c.user_id == user_id,
            tokens.c.kind == kind,
            tokens.c.expires_at.is_(None) | (tokens.c.expires_at > moment),
        )
        return int(await self.session.scalar(statement) or 0)

    async def end_oldest_logins(self, user_id: UUID, *, keep: int) -> int:
        """End the user's live logins but the ``keep`` newest; how many tokens.

        Logins are browser sessions, login tokens, and JSON Web Token logins
        (by their unused refresh token, which ends the whole login); not
        personal tokens or certificates. Newest by when they were issued.
        """
        statement = (
            select(tokens.c.id, tokens.c.family)
            .where(tokens.c.user_id == user_id, _live_login(utc_now()))
            .order_by(tokens.c.created_at.desc(), tokens.c.id.desc())
            .offset(keep)
        )
        return await self._end_logins(statement)

    async def end_other_logins(
        self, user_id: UUID, *, token: str | None = None, family: UUID | None = None
    ) -> int:
        """End the user's live logins but the one ``token`` or ``family`` names.

        Logins as in ``end_oldest_logins``; personal tokens and
        certificates stay. Under the user's credentials lock, so a login
        being issued is ended too, or sees this end.
        """
        await self.lock_user(user_id)
        statement = select(tokens.c.id, tokens.c.family).where(
            tokens.c.user_id == user_id, _live_login(utc_now())
        )
        if token is not None:
            statement = statement.where(tokens.c.id != token)
        if family is not None:
            statement = statement.where(
                tokens.c.family.is_(None) | (tokens.c.family != family)
            )
        return await self._end_logins(statement)

    async def _end_logins(self, statement: Select[str, UUID | None]) -> int:
        """End the logins of the ``(id, family)`` rows ``statement`` selects."""
        found = (await self.session.execute(statement)).all()
        if not found:
            return 0
        digests = [digest for digest, family in found if family is None]
        families = [family for _, family in found if family is not None]
        return await self._revoke(
            tokens.c.id.in_(digests) | tokens.c.family.in_(families),
            *(family_target(family) for family in families),
        )

    async def end_oldest(self, user_id: UUID, kind: TokenKind, *, keep: int) -> int:
        """End the user's tokens of ``kind`` but the ``keep`` newest; how many."""
        newest = (
            select(tokens.c.id)
            .where(tokens.c.user_id == user_id, tokens.c.kind == kind)
            .order_by(tokens.c.created_at.desc(), tokens.c.id.desc())
            .offset(keep)
        )
        oldest = list(await self.session.scalars(newest))
        if not oldest:
            return 0
        return await self._revoke(tokens.c.id.in_(oldest))

    async def of_user(
        self, user_id: UUID, *, limit: int, before: tuple[datetime, str] | None = None
    ) -> Page[AuthToken]:
        """The user's tokens, newest first, after the position ``before``.

        Not the devices that logged in (``auth.devices``): they authenticate
        nothing, and end with every token of the user.
        """
        statement = (
            select(AuthToken)
            .where(tokens.c.user_id == user_id, tokens.c.kind != TokenKind.DEVICE)
            .order_by(tokens.c.created_at.desc(), tokens.c.id.desc())
            .limit(limit + 1)
        )
        if before is not None:
            created, token_id = before
            statement = statement.where(
                (tokens.c.created_at < created)
                | ((tokens.c.created_at == created) & (tokens.c.id < token_id))
            )
        return Page.from_items(list(await self.session.scalars(statement)), limit)

    async def family_active(self, family: UUID, now: datetime | None = None) -> bool:
        """Whether the login ``family`` still has an unexpired token."""
        moment = now or utc_now()
        statement = select(
            exists().where(
                tokens.c.family == family,
                tokens.c.expires_at.is_(None) | (tokens.c.expires_at > moment),
            )
        )
        return bool(await self.session.scalar(statement))

    async def mark_used(self, token: AuthToken) -> bool:
        """Mark a refresh token used, unless a concurrent request did first."""
        when = utc_now()
        result = await self.session.execute(
            update(tokens)
            .where(tokens.c.id == token.id, tokens.c.used_at.is_(None))
            .values(used_at=when)
            .execution_options(**HANDLED)  # Logins rest on tokens, not their use.
        )
        set_committed_value(token, "used_at", when)
        return changed_rows(result) == 1

    async def revoke(self, token_id: str) -> int:
        """Delete the token with digest ``token_id``."""
        return await self._revoke(tokens.c.id == token_id)

    async def revoke_family(self, family: UUID) -> int:
        """Delete every token one login led to, under its user's lock."""
        owner = await self.session.scalar(
            select(tokens.c.user_id).where(tokens.c.family == family).limit(1)
        )
        if owner is not None:
            await self.lock_user(owner)
        return await self._revoke(tokens.c.family == family, family_target(family))

    async def revoke_user(
        self,
        user_id: UUID,
        *,
        keep: str | None = None,
        keep_family: UUID | None = None,
        keep_devices: bool = False,
    ) -> int:
        """Delete every token of ``user_id``, except one token or one family.

        ``keep_devices`` keeps the logins of the user's bound devices too:
        the rows that name a device (ADR 0131). Every principal of the user
        is read again after the commit, the kept tokens' too. The user's
        credentials lock is held first, so a token being issued is deleted
        too, or sees this end.
        """
        await self.lock_user(user_id)
        condition = tokens.c.user_id == user_id
        if keep is not None:
            condition &= tokens.c.id != keep
        if keep_family is not None:
            condition &= (tokens.c.family != keep_family) | tokens.c.family.is_(None)
        if keep_devices:
            condition &= tokens.c.device_id.is_(None)
        return await self._revoke(condition, user_target(user_id))

    async def attach_device(
        self,
        user_id: UUID,
        device_id: UUID,
        *,
        token: str | None = None,
        family: UUID | None = None,
    ) -> int:
        """Mark the login ``token`` or ``family`` names as ``device_id``'s.

        The login a device was bound with (ADR 0131); a login marked
        already keeps its device, and a personal token is no login. How
        many rows were marked.
        """
        if token is None and family is None:
            return 0
        names = (
            tokens.c.family == family if family is not None else tokens.c.id == token
        )
        result = await self.session.execute(
            update(tokens)
            .where(
                tokens.c.user_id == user_id,
                tokens.c.device_id.is_(None),
                (tokens.c.kind == TokenKind.SESSION) | (tokens.c.name == LOGIN_NAME),
                names,
            )
            .values(device_id=device_id)
            .execution_options(**HANDLED)  # Where a login came from: no principal.
        )
        return changed_rows(result)

    async def end_device_logins(self, user_id: UUID, device_id: UUID) -> int:
        """Delete the tokens of ``user_id`` marked as ``device_id``'s: how many."""
        await self.lock_user(user_id)
        return await self._revoke(
            (tokens.c.user_id == user_id) & (tokens.c.device_id == device_id)
        )

    async def clear_expired(self, now: datetime | None = None) -> int:
        """Delete tokens whose lifetime is over.

        No principal rests on them: every request checks expiry.
        """
        statement = (
            delete(tokens)
            .where(tokens.c.expires_at <= (now or utc_now()))
            .execution_options(**HANDLED)
        )
        return changed_rows(await self.session.execute(statement))

    async def _revoke(self, condition: Any, *targets: str) -> int:  # noqa: ANN401 - a SQL clause.
        """Delete matching tokens, and their principals once committed; how many."""
        deleted = (
            await self.session.execute(
                delete(tokens)
                .where(condition)
                .returning(tokens.c.id, tokens.c.family)
                .execution_options(**HANDLED)  # Marked below, by what it deleted.
            )
        ).all()
        dropped = set(targets)
        for digest, family in deleted:
            dropped.add(token_target(digest))
            if family is not None:
                dropped.add(family_target(family))
        if dropped:
            invalidate_after_commit(self.session, dropped)
        return len(deleted)


def _live_login(moment: datetime) -> ColumnElement[bool]:
    """Whether a row is a live login's, at ``moment``.

    A browser session, a login token, or a JSON Web Token login's unused
    refresh token; not a personal token or a certificate.
    """
    return (
        (tokens.c.kind == TokenKind.SESSION)
        | (
            tokens.c.kind.in_([TokenKind.ACCESS, TokenKind.REFRESH])
            & (tokens.c.name == LOGIN_NAME)
            & tokens.c.used_at.is_(None)
        )
    ) & (tokens.c.expires_at.is_(None) | (tokens.c.expires_at > moment))


def _origin_of(kind: str, name: str, like: AuthToken | None) -> dict[str, Any]:
    """Where a new token's login came from: ``like``'s, or this request's.

    Only a login's token records it, or one issued while ``logging_in``
    names its origin (an impersonation); a personal token records nothing.
    The device is the origin's, or the one ``note_login_device`` says signed
    this request (ADR 0131).
    """
    if like is not None:
        return {
            "method": like.method,
            "agent": like.agent,
            "ip": like.ip,
            "device_id": like.device_id,
        }
    origin = login_origin()
    if origin is None and not is_login(kind, name):
        return {}
    request = current_request()
    scope = getattr(request, "scope", {})
    headers = scope.get("headers", ())
    agent = next((value for key, value in headers if key == b"user-agent"), None)
    device = None if origin is None else origin.device_id
    return {
        "method": None if origin is None else origin.method,
        "agent": None
        if agent is None
        else agent.decode("latin-1")[:AGENT_MAX_LENGTH] or None,
        "ip": None if request is None else request.client_ip,
        "device_id": device if device is not None else scope.get(LOGIN_DEVICE),
    }
