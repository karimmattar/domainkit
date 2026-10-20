"""Storage of users, groups, and permissions (Django's managers).

Repositories add and change objects in their unit of work's session; the
caller commits (see ``AuthUnitOfWork``).
"""

import dataclasses
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import delete, exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from domainkit.conf import settings
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.db import SqlAlchemyRepository
from domainkit.ddd import F, Page, RowLock
from domainkit.ddd.query import Query
from domainkit.signing import derive_keys

from .events import UserLoggedIn
from .exceptions import GroupNameTaken, UnknownGroup, UnknownPermission, UsernameTaken
from .hashers import amake_password
from .models import (
    ERASED_USERNAME_PURPOSE,
    AbstractBaseUser,
    Group,
    GroupId,
    PasswordHash,
    Permission,
    UserId,
    erased_username,
    same_email,
)
from .settings import auth_settings
from .tables import (
    groups,
    mfa_devices,
    pending_signups,
    permissions,
    recovery_codes,
    tokens,
)
from .tokens import (
    LAST_USED_EVERY,
    AuthToken,
    TokenKind,
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

    async def _add(self, aggregate: AbstractBaseUser) -> None:
        """Store a new user, unless privacy erased its username from another.

        Raises:
            UsernameTaken: It did: the name stays taken, as a live one is.
        """
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

    async def set_password(self, user: AbstractBaseUser, password: str | None) -> None:
        """Hash and store a new password; None makes it unusable."""
        if password is None:
            user.set_unusable_password()
        else:
            user.set_password(PasswordHash(await amake_password(password)))

    def listing(self, search: str | None = None) -> Query:
        """Users by username; ``search`` keeps those whose name contains it."""
        name = F(self.model.USERNAME_FIELD)
        return Query(name.icontains(search) if search else None, ordering=[name])

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
        )

    async def active_superusers(self) -> int:
        """How many active superusers there are, locked until the commit.

        Two transactions that each demote one of the last two superusers
        would both count two; with the rows locked, the second count waits
        for the first transaction and sees what it left.
        """
        active = (F("is_superuser") == True) & (F("is_active") == True)  # noqa: E712
        return len(await self.filter(Query(active, lock=RowLock())))

    async def record_login(self, user: AbstractBaseUser) -> None:
        """Store the login time and record ``UserLoggedIn``.

        The time is written on its own, without bumping the user's
        ``version``, so two logins at once never conflict.
        """
        when = utc_now()
        table = get_user_table()
        await self.session.execute(
            update(table).where(table.c.id == user.id).values(last_login=when)
        )
        set_committed_value(user, "last_login", when)
        self._track(user)
        user.record(UserLoggedIn(user_id=user.id))


class GroupRepository(SqlAlchemyRepository[Group, GroupId]):
    """Groups, by id or name."""

    model = Group

    async def get_by_name(self, name: str) -> Group | None:
        """The group called ``name``."""
        group = await self.session.scalar(select(Group).where(groups.c.name == name))
        if group is not None:
            self._track(group)
        return group

    LISTING: Final = Query(ordering=["name"])
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
        """Record that ``token`` was used, at most once per ``LAST_USED_EVERY``.

        Concurrent requests write once: the update checks the stored time.
        """
        moment = now or utc_now()
        last = token.last_used_at
        if last is not None and moment - last < LAST_USED_EVERY:
            return False
        result = await self.session.execute(
            update(tokens)
            .where(
                tokens.c.id == token.id,
                tokens.c.last_used_at.is_(None)
                | (tokens.c.last_used_at <= moment - LAST_USED_EVERY),
            )
            .values(last_used_at=moment)
        )
        set_committed_value(token, "last_used_at", moment)
        return _count(result) == 1

    async def of_user(
        self, user_id: UUID, *, limit: int, before: tuple[datetime, str] | None = None
    ) -> Page[AuthToken]:
        """The user's tokens, newest first, after the position ``before``."""
        statement = (
            select(AuthToken)
            .where(tokens.c.user_id == user_id)
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
        )
        set_committed_value(token, "used_at", when)
        return _count(result) == 1

    async def revoke(self, token_id: str) -> int:
        """Delete the token with digest ``token_id``."""
        return await self._delete(tokens.c.id == token_id)

    async def revoke_family(self, family: UUID) -> int:
        """Delete every token one login led to."""
        return await self._delete(tokens.c.family == family)

    async def revoke_user(
        self, user_id: UUID, *, keep: str | None = None, keep_family: UUID | None = None
    ) -> int:
        """Delete every token of ``user_id``, except one token or one family."""
        condition = tokens.c.user_id == user_id
        if keep is not None:
            condition &= tokens.c.id != keep
        if keep_family is not None:
            condition &= (tokens.c.family != keep_family) | tokens.c.family.is_(None)
        return await self._delete(condition)

    async def clear_expired(self, now: datetime | None = None) -> int:
        """Delete tokens whose lifetime is over."""
        return await self._delete(tokens.c.expires_at <= (now or utc_now()))

    async def _delete(self, condition: Any) -> int:  # noqa: ANN401 - a SQL clause.
        """Delete matching tokens; return how many."""
        return _count(await self.session.execute(delete(tokens).where(condition)))


@dataclass(frozen=True, slots=True)
class TotpDevice:
    """A user's authenticator: its encrypted secret, and the last step used."""

    id: UUID
    user_id: UUID
    secret: bytes
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

    Its ``repr`` names its id alone: the rest is personal data, and a hash.
    """

    id: UUID
    username: str = dataclasses.field(repr=False)
    email: str = dataclasses.field(repr=False)
    password: PasswordHash = dataclasses.field(repr=False)
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
        password: PasswordHash,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> PendingSignup:
        """Store an account to make once ``email`` is confirmed, for ``ttl``."""
        moment = now or utc_now()
        pending = PendingSignup(
            id=uuid.uuid4(),
            username=username,
            email=email,
            password=password,
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


def _count(result: Any) -> int:  # noqa: ANN401 - a CursorResult.
    """Rows a statement changed."""
    return int(result.rowcount)
