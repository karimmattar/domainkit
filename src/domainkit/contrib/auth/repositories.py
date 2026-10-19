"""Storage of users, groups, and permissions (Django's managers).

Repositories add and change objects in their unit of work's session; the
caller commits (see ``AuthUnitOfWork``).
"""

import dataclasses
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from domainkit.contrib.contenttypes.tables import content_types
from domainkit.db import SqlAlchemyRepository
from domainkit.ddd import Page

from .events import UserLoggedIn
from .exceptions import GroupNameTaken, UnknownGroup, UnknownPermission, UsernameTaken
from .hashers import amake_password
from .models import AbstractBaseUser, Group, GroupId, PasswordHash, Permission, UserId
from .settings import auth_settings
from .tables import groups, permissions, tokens
from .tokens import AuthToken, TokenKind, new_token, token_digest, utc_now
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
        """Whether a user has ``username`` (normalized), in any case."""
        column = get_user_table().c[self.model.USERNAME_FIELD]
        name = self.model.normalize_username(username).lower()
        statement = select(exists().where(func.lower(column) == name))
        return bool(await self.session.scalar(statement))

    async def find_by_email(self, email: str) -> list[AbstractBaseUser]:
        """Users whose ``EMAIL_FIELD`` is ``email``, in any case; none without one."""
        field = self.model.EMAIL_FIELD
        if field is None or not email.strip():
            return []
        column = get_user_table().c[field]
        statement = (
            select(self.model)
            .where(func.lower(column) == email.strip().lower())
            .order_by(get_user_table().c.id)
        )
        users = list(await self.session.scalars(statement))
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

    async def page(
        self, *, limit: int, after: str | None = None, search: str | None = None
    ) -> Page[AbstractBaseUser]:
        """Users by username, after the username ``after``; ``search`` filters names.

        Usernames are unique, so one keeps the place between pages.
        """
        name = get_user_table().c[self.model.USERNAME_FIELD]
        statement = select(self.model).order_by(name).limit(limit + 1)
        if after is not None:
            statement = statement.where(name > after)
        if search:
            statement = statement.where(
                func.lower(name).contains(search.lower(), autoescape=True)
            )
        users = list(await self.session.scalars(statement))
        for user in users:
            self._track(user)
        return Page.from_items(users, limit)

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
        table = get_user_table()
        found = await self.session.execute(
            select(table.c.id)
            .where(table.c.is_superuser.is_(True), table.c.is_active.is_(True))
            .order_by(table.c.id)
            .with_for_update()
        )
        return len(found.all())

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

    async def page(self, *, limit: int, after: str | None = None) -> Page[Group]:
        """Groups by name (unique), after the name ``after``."""
        statement = select(Group).order_by(groups.c.name).limit(limit + 1)
        if after is not None:
            statement = statement.where(groups.c.name > after)
        found = list(await self.session.scalars(statement))
        for group in found:
            self._track(group)
        return Page.from_items(found, limit)

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
    ) -> str:
        """Store a new token for ``user_id`` and return it (shown only now).

        It works for ``ttl`` from ``now`` (default: the current time).
        """
        raw = new_token(auth_settings().TOKEN_PREFIX)
        now = now or utc_now()
        self.session.add(
            AuthToken(
                id=token_digest(raw),
                user_id=user_id,
                kind=kind,
                name=name,
                family=family,
                created_at=now,
                expires_at=None if ttl is None else now + ttl,
            )
        )
        return raw

    async def find(self, raw: str) -> AuthToken | None:
        """The stored token ``raw`` is, expired or not."""
        return await self.session.get(AuthToken, token_digest(raw))

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


def _count(result: Any) -> int:  # noqa: ANN401 - a CursorResult.
    """Rows a statement changed."""
    return int(result.rowcount)
