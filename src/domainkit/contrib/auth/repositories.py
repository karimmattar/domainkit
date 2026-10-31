"""Storage of users, groups, and permissions (Django's managers).

Repositories add and change objects in their unit of work's session; the
caller commits (see ``AuthUnitOfWork``).
"""

import dataclasses
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
from typing import Any, Final
from uuid import UUID

from sqlalchemy import (
    Executable,
    String,
    Subquery,
    Table,
    Uuid,
    bindparam,
    cast,
    delete,
    exists,
    func,
    insert,
    literal,
    null,
    select,
    true,
    type_coerce,
    union_all,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql.elements import ColumnElement

from domainkit.conf import settings
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.db import SqlAlchemyRepository
from domainkit.ddd import F, Page, RowLock, SoftDeletable
from domainkit.ddd.query import Selection
from domainkit.money import Money
from domainkit.signing import derive_keys

from .events import UserLoggedIn
from .exceptions import (
    GroupNameTaken,
    InvalidPassword,
    UnknownGroup,
    UnknownPermission,
    UsernameTaken,
)
from .hashers import acheck_password, amake_password
from .models import (
    ERASED_USERNAME_PURPOSE,
    UNUSABLE_PASSWORD_PREFIX,
    AbstractBaseUser,
    Group,
    GroupId,
    PasswordHash,
    Permission,
    UserId,
    check_not_reserved,
    erased_username,
    same_email,
)
from .principals import (
    HANDLED,
    PASSWORD,
    Credential,
    GrantedPermission,
    Login,
    Membership,
    Principal,
    Snapshot,
    family_target,
    invalidate_after_commit,
    shared,
    token_target,
    user_shape,
    user_target,
    withhold_password,
)
from .settings import auth_settings
from .tables import (
    group_permissions,
    groups,
    mfa_devices,
    password_history,
    pending_signups,
    permissions,
    recovery_codes,
    sca_challenges,
    step_ups,
    tokens,
    user_link_tables,
)
from .tokens import (
    LOGIN_NAME,
    AuthToken,
    TokenKind,
    last_used_every,
    new_token,
    token_digest,
    utc_now,
)
from .users import get_user_model, get_user_table


class UserRepository(SqlAlchemyRepository[AbstractBaseUser, UserId]):
    """Users of the configured model (``AUTH.USER_MODEL``)."""

    def __init__(self, session: AsyncSession) -> None:
        """Work with the user model in use."""
        super().__init__(session)
        self.model = get_user_model()

    async def get_by_natural_key(self, username: str) -> AbstractBaseUser | None:
        """The user whose ``USERNAME_FIELD`` is ``username`` (normalized)."""
        field = getattr(self.model, self.model.USERNAME_FIELD)
        username = self.model.normalize_username(username)
        user = await self.session.scalar(select(self.model).where(field == username))
        if user is not None:
            self._track(user)
        return user

    async def username_taken(self, username: str) -> bool:
        """Whether a user has ``username`` (normalized), in any case, or had it.

        A username privacy erased stays taken (``username_erased``).
        """
        column = get_user_table().c[self.model.USERNAME_FIELD]
        name = self.model.normalize_username(username).lower()
        statement = select(exists().where(func.lower(column) == name))
        return bool(await self.session.scalar(statement)) or (
            await self.username_erased(name)
        )

    async def username_erased(self, username: str) -> bool:
        """Whether privacy erased ``username`` from a user, in any case.

        Erased names are found under ``SECRET_KEY`` and its fallbacks: a name
        erased under a key no longer listed is taken no more.
        """
        if settings.SECRET_KEY is None:
            return False  # Nothing can have been erased without a key.
        column = get_user_table().c[self.model.USERNAME_FIELD]
        names = [
            erased_username(username, key=key)
            for key in derive_keys(ERASED_USERNAME_PURPOSE)
        ]
        statement = select(exists().where(column.in_(names)))
        return bool(await self.session.scalar(statement))

    async def remove(self, aggregate: AbstractBaseUser, *, hard: bool = False) -> None:
        """Delete the user, with every credential of theirs.

        A user model that is ``SoftDeletable`` keeps the row, marked deleted:
        its user is deactivated too (``set_active(False)``, recorded) and
        loses its credentials as a deleted one does, since logging in, and
        the tokens and principals of requests, read the row and its
        ``is_active``, not ``deleted_at``. Restoring it leaves it inactive
        until staff activate it again.
        """
        if isinstance(aggregate, SoftDeletable) and not hard:
            await self.forget_credentials(aggregate.id)
            aggregate.set_active(False)
        await super().remove(aggregate, hard=hard)

    async def _remove(self, aggregate: AbstractBaseUser) -> None:
        """Delete the user, with every credential of theirs (``forget_credentials``).

        Their object permissions go with the row too, where
        ``domainkit.contrib.objectpermissions`` is installed.
        """
        await self.forget_credentials(aggregate.id)
        await super()._remove(aggregate)

    async def forget_credentials(self, user_id: UUID) -> None:
        """Delete every token and the second factor of ``user_id``.

        Its sessions, personal tokens, refresh tokens, and client
        certificates, its authenticator's secret and recovery codes, its
        step-ups, its transactions' authorizations, and the hashes of its
        last passwords: secrets of an account nobody may use. Every
        principal of the user
        is read again after the commit. The caller commits.
        """
        await TokenStore(self.session).revoke_user(user_id)
        await MfaStore(self.session).remove(user_id)
        for table in (step_ups, sca_challenges, password_history):
            await self.session.execute(delete(table).where(table.c.user_id == user_id))

    async def _add(self, aggregate: AbstractBaseUser) -> None:
        """Store a new user, unless privacy erased its username from another.

        Raises:
            InvalidUsername: The name starts with ``erased-``, which erasure
                keeps for itself (for user models of any kind).
            UsernameTaken: It did: the name stays taken, as a live one is.
        """
        check_not_reserved(aggregate.get_username())
        if await self.username_erased(aggregate.get_username()):
            field = self.model.USERNAME_FIELD
            raise UsernameTaken(f"The {field} is already taken.", fields=(field,))
        await super()._add(aggregate)

    async def find_by_email(self, email: str) -> list[AbstractBaseUser]:
        """Users whose ``EMAIL_FIELD`` is ``email``, in any case; none without one.

        Only ASCII letters fold (``same_email``). SQL's ``lower()``, on both
        sides, finds the candidates: PostgreSQL's folds more than ASCII
        (the KELVIN SIGN onto ``k``), SQLite's ASCII alone, so the
        candidates are then compared one by one, the same way everywhere.
        """
        field = self.model.EMAIL_FIELD
        if field is None or not email.strip():
            return []
        column = get_user_table().c[field]
        statement = (
            select(self.model)
            .where(func.lower(column) == func.lower(email.strip()))
            .order_by(get_user_table().c.id)
        )
        users = [
            user
            for user in await self.session.scalars(statement)
            if same_email(getattr(user, field), email)
        ]
        for user in users:
            self._track(user)
        return users

    async def create_user(
        self,
        username: str,
        password: str | None = None,
        **fields: Any,  # noqa: ANN401 - the fields of each user model.
    ) -> AbstractBaseUser:
        """Add a user; without ``password``, it gets an unusable one.

        Passwords are hashed, not validated: call ``validate_password``
        first where people choose them.

        Raises:
            UsernameTaken: Another user has ``username``.
        """
        username = self.model.normalize_username(username)
        if await self.get_by_natural_key(username) is not None:
            raise UsernameTaken(f"The username {username!r} is taken.")
        hashed = (
            PasswordHash(await amake_password(password))
            if password is not None
            else PasswordHash.unusable()
        )
        user = self.model.create(
            password=hashed, **{**fields, self.model.USERNAME_FIELD: username}
        )
        await self.add(user)
        if password is not None:
            await PasswordHistoryStore(self.session).record(user.id, hashed)
        return user

    async def create_superuser(
        self,
        username: str,
        password: str | None = None,
        **fields: Any,  # noqa: ANN401 - the fields of each user model.
    ) -> AbstractBaseUser:
        """Add a user with every permission (and staff, if the model has it)."""
        fields["is_superuser"] = True
        if "is_staff" in {field.name for field in dataclasses.fields(self.model)}:
            fields.setdefault("is_staff", True)
        return await self.create_user(username, password, **fields)

    async def set_password(
        self,
        user: AbstractBaseUser,
        password: str | None,
        *,
        priority: bool = False,
        temporary: bool = False,
    ) -> None:
        """Hash and store a new password; None makes it unusable.

        ``priority`` hashes in the places ``AUTH.HASHING_RESERVED`` keeps,
        for signed-in users changing a password. With
        ``AUTH.PASSWORD_HISTORY``, a password among the user's last ones
        (the current one included) is refused; with it or
        ``AUTH.PASSWORD_CHANGE_ON_FIRST_USE``, the new one is kept in the
        history, ``temporary`` (set by staff, to change at the next login)
        or not.

        Raises:
            InvalidPassword: The password is one of the user's last ones.
        """
        if password is None:
            user.set_unusable_password()
            return
        history = PasswordHistoryStore(self.session)
        await history.refuse_reuse(user.id, password, priority=priority)
        hashed = PasswordHash(await amake_password(password, priority=priority))
        user.set_password(hashed)
        await history.record(user.id, hashed, temporary=temporary)

    def listing(self, search: str | None = None) -> Selection:
        """Users by username; ``search`` keeps those whose name contains it."""
        name = F(self.model.USERNAME_FIELD)
        return Selection(name.icontains(search) if search else None, ordering=[name])

    async def page(
        self, *, limit: int, after: str | None = None, search: str | None = None
    ) -> Page[AbstractBaseUser]:
        """Users by username, after the username ``after``; ``search`` filters names.

        Usernames are unique, so one keeps the place between pages.
        """
        query = self.listing(search)
        if after is not None:
            query = query.where(F(self.model.USERNAME_FIELD) > after)
        return await self.paginate(query, limit=limit)

    async def store_rehash(
        self, user_id: UUID, verified: PasswordHash, rehashed: PasswordHash
    ) -> None:
        """Replace the ``verified`` hash with ``rehashed``, the same password.

        It does not bump the user's ``version``, so logins that rehash at
        once never conflict. A hash changed meanwhile (a new password) is
        left as it is.
        """
        table = get_user_table()
        await self.session.execute(
            update(table)
            .where(table.c.id == user_id, table.c.password == verified)
            .values(password=rehashed)
            .execution_options(**HANDLED)  # The same password: principals hold.
        )

    async def active_superusers(self) -> int:
        """How many active superusers there are, locked until the commit.

        Two transactions that each demote one of the last two superusers
        would both count two; with the rows locked, the second count waits
        for the first transaction and sees what it left.
        """
        active = (F("is_superuser") == True) & (F("is_active") == True)  # noqa: E712
        return len(await self.filter(Selection(active, lock=RowLock())))

    async def record_login(self, user: AbstractBaseUser) -> None:
        """Store the login time and record ``UserLoggedIn``.

        The time is written on its own, without bumping the user's
        ``version``, so two logins at once never conflict.
        """
        when = utc_now()
        table = get_user_table()
        await self.session.execute(
            update(table)
            .where(table.c.id == user.id)
            .values(last_login=when)
            .execution_options(**HANDLED)  # Marked below.
        )
        set_committed_value(user, "last_login", when)
        invalidate_after_commit(self.session, [user_target(user.id)])
        self._track(user)
        user.record(UserLoggedIn(user_id=user.id))

    async def password_hash(self, user_id: UUID) -> PasswordHash | None:
        """The stored password hash of ``user_id``, read on its own.

        Users that authenticated requests carry none (``request.user``):
        read it here to check a password.
        """
        table = get_user_table()
        hashed: PasswordHash | None = await self.session.scalar(
            select(table.c[PASSWORD]).where(table.c.id == user_id)
        )
        return hashed


class GroupRepository(SqlAlchemyRepository[Group, GroupId]):
    """Groups, by id or name."""

    model = Group

    async def get_by_name(self, name: str) -> Group | None:
        """The group called ``name``."""
        group = await self.session.scalar(select(Group).where(groups.c.name == name))
        if group is not None:
            self._track(group)
        return group

    LISTING: Final = Selection(ordering=["name"])
    """Groups by name (unique)."""

    async def page(self, *, limit: int, after: str | None = None) -> Page[Group]:
        """Groups by name (unique), after the name ``after``."""
        query = self.LISTING
        if after is not None:
            query = query.where(F("name") > after)
        return await self.paginate(query, limit=limit)

    async def get_many_by_name(self, names: Iterable[str]) -> list[Group]:
        """The groups ``names`` name, in that order.

        Raises:
            UnknownGroup: One of the names names no group.
        """
        wanted = list(dict.fromkeys(names))
        statement = select(Group).where(groups.c.name.in_(wanted))
        found = {group.name: group for group in await self.session.scalars(statement)}
        if missing := [name for name in wanted if name not in found]:
            raise UnknownGroup(f"No such group: {', '.join(missing)}.")
        for group in found.values():
            self._track(group)
        return [found[name] for name in wanted]

    async def rename(self, group: Group, name: str) -> None:
        """Give ``group`` a new name.

        Raises:
            GroupNameTaken: Another group has the name.
        """
        group.rename(name)
        with self.session.no_autoflush:
            same = await self.get_by_name(group.name)
        if same is not None and same is not group:
            raise GroupNameTaken(f"A group called {group.name!r} exists.")

    async def create(self, name: str) -> Group:
        """Add a group with no permissions.

        Raises:
            GroupNameTaken: Another group has the name.
        """
        group = Group.create(name)
        if await self.get_by_name(group.name) is not None:
            raise GroupNameTaken(f"A group called {group.name!r} exists.")
        await self.add(group)
        return group


class PermissionStore:
    """Permissions, by key (``<domain>.<codename>``); ``migrate`` creates them."""

    def __init__(self, session: AsyncSession) -> None:
        """Read in ``session``."""
        self.session = session

    async def get(self, key: str) -> Permission:
        """The permission ``key`` names.

        Raises:
            UnknownPermission: No permission has that key.
        """
        (permission,) = await self.get_many([key])
        return permission

    async def get_many(self, keys: Iterable[str]) -> list[Permission]:
        """The permissions ``keys`` name, in that order.

        Raises:
            UnknownPermission: One of the keys names no permission.
        """
        wanted = list(dict.fromkeys(keys))
        found = {permission.key: permission for permission in await self.all()}
        if missing := [key for key in wanted if key not in found]:
            raise UnknownPermission(f"No such permission: {', '.join(missing)}.")
        return [found[key] for key in wanted]

    async def all(self) -> list[Permission]:
        """Every permission, by domain, content type, and codename."""
        statement = (
            select(Permission)
            .join(content_types)  # On the permission's foreign key.
            .order_by(
                content_types.c.domain, content_types.c.model, permissions.c.codename
            )
        )
        return list(await self.session.scalars(statement))

    async def keys(self) -> frozenset[str]:
        """The key of every permission."""
        return frozenset(permission.key for permission in await self.all())


class TokenStore:
    """Opaque tokens, found by their digest; the raw token is never stored."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

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
    ) -> str:
        """Store a new token for ``user_id`` and return it (shown only now).

        It works for ``ttl`` from ``now`` (default: the current time), with
        the permission keys in ``scopes`` only, if given.
        """
        raw, _ = await self.create(
            user_id,
            kind=kind,
            ttl=ttl,
            name=name,
            family=family,
            now=now,
            scopes=scopes,
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
    ) -> tuple[str, AuthToken]:
        """``issue``, returning the stored token with the raw one."""
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
        return _count(result) == 1

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
        moment = utc_now()
        statement = (
            select(tokens.c.id, tokens.c.family)
            .where(
                tokens.c.user_id == user_id,
                (tokens.c.kind == TokenKind.SESSION)
                | (
                    tokens.c.kind.in_([TokenKind.ACCESS, TokenKind.REFRESH])
                    & (tokens.c.name == LOGIN_NAME)
                    & tokens.c.used_at.is_(None)
                ),
                tokens.c.expires_at.is_(None) | (tokens.c.expires_at > moment),
            )
            .order_by(tokens.c.created_at.desc(), tokens.c.id.desc())
            .offset(keep)
        )
        oldest = (await self.session.execute(statement)).all()
        if not oldest:
            return 0
        digests = [digest for digest, family in oldest if family is None]
        families = [family for _, family in oldest if family is not None]
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
        return _count(result) == 1

    async def revoke(self, token_id: str) -> int:
        """Delete the token with digest ``token_id``."""
        return await self._revoke(tokens.c.id == token_id)

    async def revoke_family(self, family: UUID) -> int:
        """Delete every token one login led to."""
        return await self._revoke(tokens.c.family == family, family_target(family))

    async def revoke_user(
        self, user_id: UUID, *, keep: str | None = None, keep_family: UUID | None = None
    ) -> int:
        """Delete every token of ``user_id``, except one token or one family.

        Every principal of the user is read again after the commit, the
        kept token's too.
        """
        condition = tokens.c.user_id == user_id
        if keep is not None:
            condition &= tokens.c.id != keep
        if keep_family is not None:
            condition &= (tokens.c.family != keep_family) | tokens.c.family.is_(None)
        return await self._revoke(condition, user_target(user_id))

    async def clear_expired(self, now: datetime | None = None) -> int:
        """Delete tokens whose lifetime is over.

        No principal rests on them: every request checks expiry.
        """
        statement = (
            delete(tokens)
            .where(tokens.c.expires_at <= (now or utc_now()))
            .execution_options(**HANDLED)
        )
        return _count(await self.session.execute(statement))

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


GRANT_COLUMNS: Final = 9
"""Columns of each grant row ``PrincipalStore.principal`` reads after the user's."""
CREDENTIAL: Final = select(
    tokens.c.id,
    tokens.c.user_id,
    tokens.c.kind,
    tokens.c.family,
    tokens.c.expires_at,
    tokens.c.scopes,
    tokens.c.last_used_at,
    tokens.c.name,
    tokens.c.created_at,
).where(tokens.c.id == bindparam("digest"))
"""What the token table says of one token (built once: no cache key to make)."""
LOGIN: Final = select(
    func.count(), func.count(tokens.c.expires_at), func.max(tokens.c.expires_at)
).where(tokens.c.family == bindparam("family"))
"""How many tokens a login has, how many expire, and when the last does."""


class PrincipalStore:
    """What authenticating a request reads: a credential, then its principal.

    Two queries for a credential: the token by its digest (a login's tokens,
    or a user by name), then the user, without the password hash, with
    their groups and permissions, in one statement (``principal``). Each
    statement is built once and reused, with bound values.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Read in ``session``."""
        self.session = session

    async def credential(self, digest: str) -> Credential | None:
        """What the token table says of token ``digest``, if it is stored."""
        row = (await self.session.execute(CREDENTIAL, {"digest": digest})).first()
        return None if row is None else Credential(**row._mapping)

    async def login(self, family: UUID) -> Login:
        """Whether the login ``family`` has tokens, and when the last expires."""
        count, expiring, until = (
            await self.session.execute(LOGIN, {"family": family})
        ).one()
        return Login(active=count > 0, until=None if expiring < count else until)

    async def user_id(self, username: str) -> UUID | None:
        """The id of the user whose ``USERNAME_FIELD`` is ``username`` (normalized)."""
        model = get_user_model()
        statement = _user_by_name(get_user_table(), model.USERNAME_FIELD)
        name = model.normalize_username(username)
        found: UUID | None = await self.session.scalar(statement, {"username": name})
        return found

    async def principal(
        self, user_id: UUID, *, token: str | None = None, username: str | None = None
    ) -> Snapshot:
        """User ``user_id``, with their groups and permissions, in one statement.

        The same statement checks that ``token`` is still stored and that
        ``username`` (normalized) is still the user's: ``current`` says.
        Not the password hash: only whether it is usable
        (``withhold_password``). A model a snapshot cannot hold
        (``user_shape``) is loaded by the ORM, the hash deferred.
        """
        model = get_user_model()
        shape = user_shape(model)
        statement = _principal_statement(
            model, get_user_table(), token is not None, username is not None
        )
        values: dict[str, object] = {"user_id": user_id}
        if token is not None:
            values["token"] = token
        if username is not None:
            values["username"] = model.normalize_username(username)
        if shape is None:
            row = (await self.session.execute(statement, values)).first()
            if row is None:
                return Snapshot()
            loaded: AbstractBaseUser = row[0]
            withhold_password(loaded, usable=bool(row[1]))
            return Snapshot(loaded=loaded, current=row[2])
        rows = (await self.session.execute(statement, values)).all()
        if not rows:
            return Snapshot()
        width = len(shape.columns)
        columns = tuple(
            (key, rows[0][index]) for index, (key, _) in enumerate(shape.columns)
        )
        usable = bool(rows[0][width])
        found: dict[UUID, tuple[str, int, dict[int, GrantedPermission]]] = {}
        direct: dict[int, GrantedPermission] = {}
        for row in rows:
            group, name, version, *grant = row[width + 1 : width + 1 + GRANT_COLUMNS]
            permission = None if grant[0] is None else shared(GrantedPermission(*grant))
            if group is not None:
                held = found.setdefault(group, (name, version, {}))[2]
                if permission is not None:
                    held[permission.id] = permission
            elif permission is not None:
                direct[permission.id] = permission
        principal = Principal(
            model=model,
            is_active=dict(columns)["is_active"] is True,
            columns=columns,
            groups=tuple(
                shared(Membership(group, name, version, _sorted(held)))
                for group, (name, version, held) in found.items()
            ),
            permissions=_sorted(direct),
            usable_password=usable,
        )
        return Snapshot(principal=principal, current=bool(rows[0][-1]))


def _sorted(granted: dict[int, GrantedPermission]) -> tuple[GrantedPermission, ...]:
    """``granted`` by id, as one shared tuple: equal grants are one object."""
    return shared(tuple(granted[key] for key in sorted(granted)))


@cache
def _user_by_name(user_table: Table, field: str) -> Executable:
    """The id of the user named ``:username`` (built once per table)."""
    return select(user_table.c.id).where(user_table.c[field] == bindparam("username"))


@cache
def _principal_statement(
    model: type[AbstractBaseUser], user_table: Table, token: bool, username: bool
) -> Executable:
    """``PrincipalStore.principal``'s statement, built once per model and checks.

    It takes ``:user_id``, and ``:token`` and ``:username`` when it checks
    them.
    """
    conditions = [user_table.c.id == bindparam("user_id")]
    if username:
        conditions.append(user_table.c[model.USERNAME_FIELD] == bindparam("username"))
    live = (
        exists().where(tokens.c.id == bindparam("token")) if token else literal(True)
    ).label("current")
    shape = user_shape(model)
    if shape is None:
        return (
            select(model, _usable(user_table.c[PASSWORD]), live)
            .options(defer(getattr(model, PASSWORD)))
            .where(*conditions)
        )
    granted = _grants(user_table)
    return (
        select(
            *(column.label(f"u{i}") for i, (_, column) in enumerate(shape.columns)),
            _usable(shape.password),
            *granted.c,
            live,
        )
        .select_from(user_table.outerjoin(granted, true()))
        .where(*conditions)
    )


def _usable(password: ColumnElement[Any]) -> ColumnElement[bool]:
    """Whether a stored password is usable, read without the hash itself."""
    return (~type_coerce(password, String).startswith(UNUSABLE_PASSWORD_PREFIX)).label(
        "usable"
    )


def _grants(user_table: Table) -> Subquery:
    """Rows of ``:user_id``'s groups (with each permission) and own permissions.

    One row per group and permission it gives, one per group that gives
    none, and one per permission given directly (``g_*`` columns null).
    """
    user_groups, user_permissions = user_link_tables(user_table)
    of_groups = (
        select(
            groups.c.id.label("g_id"),
            groups.c.name.label("g_name"),
            groups.c.version.label("g_version"),
            permissions.c.id.label("p_id"),
            permissions.c.codename.label("p_codename"),
            permissions.c.name.label("p_name"),
            content_types.c.id.label("ct_id"),
            content_types.c.domain.label("ct_domain"),
            content_types.c.model.label("ct_model"),
        )
        .select_from(
            user_groups.join(groups, groups.c.id == user_groups.c.group_id)
            .outerjoin(group_permissions, group_permissions.c.group_id == groups.c.id)
            .outerjoin(
                permissions, permissions.c.id == group_permissions.c.permission_id
            )
            .outerjoin(
                content_types, content_types.c.id == permissions.c.content_type_id
            )
        )
        .where(user_groups.c.user_id == bindparam("user_id"))
    )
    direct = (
        select(
            cast(null(), Uuid),
            null(),
            null(),
            permissions.c.id,
            permissions.c.codename,
            permissions.c.name,
            content_types.c.id,
            content_types.c.domain,
            content_types.c.model,
        )
        .select_from(
            user_permissions.join(
                permissions, permissions.c.id == user_permissions.c.permission_id
            ).join(content_types, content_types.c.id == permissions.c.content_type_id)
        )
        .where(user_permissions.c.user_id == bindparam("user_id"))
    )
    return union_all(of_groups, direct).subquery("granted")


@dataclass(frozen=True, slots=True)
class TotpDevice:
    """A user's authenticator: its encrypted secret, and the last step used."""

    id: UUID
    user_id: UUID
    secret: bytes = dataclasses.field(repr=False)
    """Encrypted; ``mfa.secret_box()`` opens it."""
    created_at: datetime
    confirmed_at: datetime | None
    last_step: int | None

    @property
    def confirmed(self) -> bool:
        """Whether a code proved the app holds the secret."""
        return self.confirmed_at is not None


class MfaStore:
    """Authenticators and recovery codes, by user."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def device(self, user_id: UUID) -> TotpDevice | None:
        """The user's authenticator, confirmed or not."""
        row = (
            await self.session.execute(
                select(mfa_devices).where(mfa_devices.c.user_id == user_id)
            )
        ).first()
        return None if row is None else TotpDevice(**row._mapping)

    async def enabled(self, user_id: UUID) -> bool:
        """Whether the user has a confirmed authenticator."""
        statement = select(
            exists().where(
                mfa_devices.c.user_id == user_id,
                mfa_devices.c.confirmed_at.is_not(None),
            )
        )
        return bool(await self.session.scalar(statement))

    async def start(self, user_id: UUID, secret: bytes) -> UUID:
        """Store a new, unconfirmed authenticator, replacing an unconfirmed one."""
        await self.session.execute(
            delete(mfa_devices).where(
                mfa_devices.c.user_id == user_id, mfa_devices.c.confirmed_at.is_(None)
            )
        )
        device_id = uuid.uuid4()
        await self.session.execute(
            insert(mfa_devices).values(
                id=device_id, user_id=user_id, secret=secret, created_at=utc_now()
            )
        )
        return device_id

    async def confirm(self, device_id: UUID, step: int) -> bool:
        """Confirm the authenticator with the step of its first code."""
        result = await self.session.execute(
            update(mfa_devices)
            .where(mfa_devices.c.id == device_id, mfa_devices.c.confirmed_at.is_(None))
            .values(confirmed_at=utc_now(), last_step=step)
        )
        return _count(result) == 1

    async def use_step(self, device_id: UUID, step: int) -> bool:
        """Record ``step`` as used, unless it, or a later one, already was."""
        result = await self.session.execute(
            update(mfa_devices)
            .where(
                mfa_devices.c.id == device_id,
                mfa_devices.c.last_step.is_(None) | (mfa_devices.c.last_step < step),
            )
            .values(last_step=step)
        )
        return _count(result) == 1

    async def reseal(self, device_id: UUID, secret: bytes) -> None:
        """Store the secret encrypted again (under the current key)."""
        await self.session.execute(
            update(mfa_devices)
            .where(mfa_devices.c.id == device_id)
            .values(secret=secret)
        )

    async def remove(self, user_id: UUID) -> int:
        """Delete the user's authenticator and recovery codes; return how many."""
        removed = await self.session.execute(
            delete(mfa_devices).where(mfa_devices.c.user_id == user_id)
        )
        await self.session.execute(
            delete(recovery_codes).where(recovery_codes.c.user_id == user_id)
        )
        return _count(removed)

    async def replace_codes(self, user_id: UUID, digests: Iterable[str]) -> None:
        """Make ``digests`` the user's only recovery codes."""
        await self.session.execute(
            delete(recovery_codes).where(recovery_codes.c.user_id == user_id)
        )
        await self.session.execute(
            insert(recovery_codes),
            [{"id": digest, "user_id": user_id} for digest in digests],
        )

    async def use_code(self, user_id: UUID, digest: str) -> bool:
        """Spend the user's unused recovery code with ``digest``, once."""
        result = await self.session.execute(
            update(recovery_codes)
            .where(
                recovery_codes.c.id == digest,
                recovery_codes.c.user_id == user_id,
                recovery_codes.c.used_at.is_(None),
            )
            .values(used_at=utc_now())
        )
        return _count(result) == 1

    async def codes_left(self, user_id: UUID) -> int:
        """How many of the user's recovery codes are unused."""
        statement = select(func.count()).where(
            recovery_codes.c.user_id == user_id, recovery_codes.c.used_at.is_(None)
        )
        return int(await self.session.scalar(statement) or 0)


@dataclass(frozen=True, slots=True)
class PendingSignup:
    """An account someone asked for at ``/signup``, not made yet.

    It holds no password: whoever confirms the address chooses one. Its
    ``repr`` names its id alone: the rest is personal data.
    """

    id: UUID
    username: str = dataclasses.field(repr=False)
    email: str = dataclasses.field(repr=False)
    created_at: datetime = dataclasses.field(repr=False)
    expires_at: datetime = dataclasses.field(repr=False)

    def expired(self, now: datetime | None = None) -> bool:
        """Whether its link no longer works."""
        return self.expires_at <= (now or utc_now())


class SignupStore:
    """Pending signups, by id; nothing here makes or reserves an account."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def add(
        self,
        *,
        username: str,
        email: str,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> PendingSignup:
        """Store an account to make once ``email`` is confirmed, for ``ttl``."""
        moment = now or utc_now()
        pending = PendingSignup(
            id=uuid.uuid4(),
            username=username,
            email=email,
            created_at=moment,
            expires_at=moment + ttl,
        )
        await self.session.execute(
            insert(pending_signups).values(
                {
                    field.name: getattr(pending, field.name)
                    for field in dataclasses.fields(pending)
                }
            )
        )
        return pending

    async def get(self, signup_id: UUID) -> PendingSignup | None:
        """The pending signup ``signup_id``, expired or not."""
        row = (
            await self.session.execute(
                select(pending_signups).where(pending_signups.c.id == signup_id)
            )
        ).first()
        return None if row is None else PendingSignup(**row._mapping)

    async def remove(self, signup_id: UUID) -> bool:
        """Delete the pending signup; False if it was gone (taken by another)."""
        result = await self.session.execute(
            delete(pending_signups).where(pending_signups.c.id == signup_id)
        )
        return _count(result) == 1

    async def clear_expired(self, now: datetime | None = None) -> int:
        """Delete pending signups whose link no longer works; return how many."""
        result = await self.session.execute(
            delete(pending_signups).where(
                pending_signups.c.expires_at <= (now or utc_now())
            )
        )
        return _count(result)


class StepUpStore:
    """The last step-up of each login token and browser session, by digest."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def record(
        self, digest: str, user_id: UUID, when: datetime, methods: Iterable[str]
    ) -> None:
        """Keep that the credential ``digest`` of ``user_id`` stepped up at ``when``."""
        await self.session.execute(delete(step_ups).where(step_ups.c.id == digest))
        await self.session.execute(
            insert(step_ups).values(
                id=digest,
                user_id=user_id,
                auth_time=when,
                amr=" ".join(sorted(set(methods))),
            )
        )

    async def last(
        self, digest: str, user_id: UUID
    ) -> tuple[datetime, frozenset[str]] | None:
        """When credential ``digest`` of ``user_id`` last stepped up, and with what."""
        row = (
            await self.session.execute(
                select(step_ups.c.auth_time, step_ups.c.amr).where(
                    step_ups.c.id == digest, step_ups.c.user_id == user_id
                )
            )
        ).first()
        return None if row is None else (row.auth_time, frozenset(row.amr.split()))

    async def clear_older(self, before: datetime) -> int:
        """Delete step-ups from before ``before``: no rule takes them; how many."""
        result = await self.session.execute(
            delete(step_ups).where(step_ups.c.auth_time < before)
        )
        return _count(result)


@dataclass(frozen=True, slots=True)
class ScaChallenge:
    """A transaction waiting for its user's authorization, or holding it.

    Its ``repr`` names its id and state alone: the payee may be personal data.
    """

    id: UUID
    user_id: UUID
    amount: Money = dataclasses.field(repr=False)
    payee: str = dataclasses.field(repr=False)
    purpose: str
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None = None
    amr: str | None = None
    """What confirmed it, RFC 8176's names separated by spaces."""
    consumed_at: datetime | None = None

    def expired(self, now: datetime | None = None) -> bool:
        """Whether it no longer works."""
        return self.expires_at <= (now or utc_now())


class ScaStore:
    """Transactions' authorizations, by id (``auth.sca``)."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def add(
        self,
        user_id: UUID,
        amount: Money,
        payee: str,
        purpose: str,
        *,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> ScaChallenge:
        """Store a transaction for ``user_id`` to authorize within ``ttl``."""
        moment = now or utc_now()
        challenge = ScaChallenge(
            id=uuid.uuid4(),
            user_id=user_id,
            amount=amount,
            payee=payee,
            purpose=purpose,
            created_at=moment,
            expires_at=moment + ttl,
        )
        await self.session.execute(
            insert(sca_challenges).values(
                id=challenge.id,
                user_id=user_id,
                amount=amount.amount,
                amount_currency=amount.currency.code,
                payee=payee,
                purpose=purpose,
                created_at=moment,
                expires_at=challenge.expires_at,
            )
        )
        return challenge

    async def get(self, challenge_id: UUID) -> ScaChallenge | None:
        """The transaction ``challenge_id``, in any state."""
        row = (
            await self.session.execute(
                select(sca_challenges).where(sca_challenges.c.id == challenge_id)
            )
        ).first()
        if row is None:
            return None
        values = dict(row._mapping)
        amount = Money(values.pop("amount"), values.pop("amount_currency"))
        return ScaChallenge(amount=amount, **values)

    async def confirm(
        self, challenge_id: UUID, when: datetime, methods: Iterable[str]
    ) -> bool:
        """Mark it confirmed at ``when``, if it is unconfirmed, unused, and live."""
        result = await self.session.execute(
            update(sca_challenges)
            .where(
                sca_challenges.c.id == challenge_id,
                sca_challenges.c.confirmed_at.is_(None),
                sca_challenges.c.consumed_at.is_(None),
                sca_challenges.c.expires_at > when,
            )
            .values(confirmed_at=when, amr=" ".join(sorted(set(methods))))
        )
        return _count(result) == 1

    async def consume(self, challenge_id: UUID, when: datetime) -> bool:
        """Mark it used at ``when``, if it is unused; one caller wins."""
        result = await self.session.execute(
            update(sca_challenges)
            .where(
                sca_challenges.c.id == challenge_id,
                sca_challenges.c.consumed_at.is_(None),
            )
            .values(consumed_at=when)
        )
        return _count(result) == 1

    async def clear_expired(self, now: datetime | None = None) -> int:
        """Delete transactions whose authorization no longer works; how many."""
        result = await self.session.execute(
            delete(sca_challenges).where(
                sca_challenges.c.expires_at <= (now or utc_now())
            )
        )
        return _count(result)


class PasswordHistoryStore:
    """Each user's last password hashes (``AUTH.PASSWORD_HISTORY``).

    Kept only while ``PASSWORD_HISTORY`` or ``PASSWORD_CHANGE_ON_FIRST_USE``
    is set: without them nothing is written, read, or checked.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def refuse_reuse(
        self, user_id: UUID, password: str, *, priority: bool = False
    ) -> None:
        """Refuse ``password`` if it is one of the user's last ``PASSWORD_HISTORY``.

        The current password counts among them, kept or not.

        Raises:
            InvalidPassword: It is.
        """
        count = auth_settings().PASSWORD_HISTORY
        if not count:
            return
        hashes = {each.value for each in await self._newest(user_id, count)}
        current = await UserRepository(self.session).password_hash(user_id)
        if current is not None and current.usable:
            hashes.add(current.value)
        for hashed in sorted(hashes):
            if await acheck_password(password, hashed, priority=priority):
                reused = f"This password is one of your last {count}; choose one "
                raise InvalidPassword([reused + "you have not used."])

    async def record(
        self, user_id: UUID, hashed: PasswordHash, *, temporary: bool = False
    ) -> None:
        """Keep ``hashed`` as the user's newest password, dropping the oldest."""
        config = auth_settings()
        keep = config.PASSWORD_HISTORY
        if not (keep or config.PASSWORD_CHANGE_ON_FIRST_USE):
            return
        await self.session.execute(
            insert(password_history).values(
                user_id=user_id,
                password=hashed,
                created_at=utc_now(),
                temporary=temporary,
            )
        )
        newest = (
            select(password_history.c.id)
            .where(password_history.c.user_id == user_id)
            .order_by(password_history.c.id.desc())
            .limit(max(keep, 1))
        )
        await self.session.execute(
            delete(password_history).where(
                password_history.c.user_id == user_id,
                password_history.c.id.not_in(newest.scalar_subquery()),
            )
        )

    async def must_change(self, user_id: UUID) -> bool:
        """Whether the user's current password was set by staff, to change."""
        newest = (
            await self.session.execute(
                select(password_history.c.temporary)
                .where(password_history.c.user_id == user_id)
                .order_by(password_history.c.id.desc())
                .limit(1)
            )
        ).scalar()
        return newest is True

    async def require_change(self, user_id: UUID) -> None:
        """Mark the user's current password as set by staff, to change.

        Only with ``AUTH.PASSWORD_CHANGE_ON_FIRST_USE``: nothing otherwise.
        """
        if not auth_settings().PASSWORD_CHANGE_ON_FIRST_USE:
            return
        newest = (
            select(password_history.c.id)
            .where(password_history.c.user_id == user_id)
            .order_by(password_history.c.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        await self.session.execute(
            update(password_history)
            .where(password_history.c.id == newest)
            .values(temporary=True)
        )

    async def _newest(self, user_id: UUID, count: int) -> list[PasswordHash]:
        """The user's ``count`` newest kept hashes."""
        rows = await self.session.scalars(
            select(password_history.c.password)
            .where(password_history.c.user_id == user_id)
            .order_by(password_history.c.id.desc())
            .limit(count)
        )
        return list(rows)


def _count(result: Any) -> int:  # noqa: ANN401 - a CursorResult.
    """Rows a statement changed."""
    return int(result.rowcount)
