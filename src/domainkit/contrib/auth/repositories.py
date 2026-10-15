"""Storage of users, groups, and permissions (Django's managers).

Repositories add and change objects in their unit of work's session; the
caller commits (see ``AuthUnitOfWork``).
"""

import dataclasses
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.contrib.contenttypes.tables import content_types
from domainkit.db import SqlAlchemyRepository

from .exceptions import GroupNameTaken, UnknownPermission, UsernameTaken
from .hashers import amake_password
from .models import AbstractBaseUser, Group, GroupId, PasswordHash, Permission, UserId
from .tables import groups, permissions
from .users import get_user_model


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


class GroupRepository(SqlAlchemyRepository[Group, GroupId]):
    """Groups, by id or name."""

    model = Group

    async def get_by_name(self, name: str) -> Group | None:
        """The group called ``name``."""
        group = await self.session.scalar(select(Group).where(groups.c.name == name))
        if group is not None:
            self._track(group)
        return group

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
