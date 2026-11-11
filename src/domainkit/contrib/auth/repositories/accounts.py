"""Users, groups, permissions, and the passwords a user had."""

import dataclasses
import logging
from collections.abc import Awaitable, Callable, Iterable
from functools import reduce
from operator import or_
from typing import Any, Final
from uuid import UUID

from sqlalchemy import delete, exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from domainkit.conf import settings
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.db import SqlAlchemyRepository
from domainkit.db.encrypted import EncryptedType
from domainkit.ddd import F, Page, RowLock, SoftDeletable
from domainkit.ddd.query import Criteria, Selection
from domainkit.log import SECURITY_LOGGER
from domainkit.signing import derive_keys

from ..events import UserLoggedIn
from ..exceptions import (
    EmailTaken,
    GroupNameTaken,
    IdentifierRequired,
    InvalidPassword,
    UnknownGroup,
    UnknownPermission,
    UsernameTaken,
)
from ..hashers import acheck_password, amake_password, identify_hasher
from ..models import (
    ERASED_USERNAME_PURPOSE,
    AbstractBaseUser,
    Group,
    GroupId,
    PasswordHash,
    Permission,
    UserId,
    as_mobile_number,
    check_not_reserved,
    email_key,
    erased_username,
    same_email,
)
from ..principals import HANDLED, PASSWORD, invalidate_after_commit, user_target
from ..settings import auth_settings, identifier_fields
from ..tables import (
    auth_codes,
    groups,
    password_history,
    permissions,
    sca_challenges,
    step_ups,
)
from ..tokens import utc_now
from ..users import get_user_model, get_user_table
from .factors import MfaStore
from .tokenstore import TokenStore

security_logger = logging.getLogger(SECURITY_LOGGER)


MOBILE_NUMBER: Final = "mobile_number"
"""The field that holds a mobile number, where one identifies users."""


class UserRepository(SqlAlchemyRepository[AbstractBaseUser, UserId]):
    """Users of the configured model (``AUTH.USER_MODEL``)."""

    def __init__(self, session: AsyncSession) -> None:
        """Work with the user model in use."""
        super().__init__(session)
        self.model = get_user_model()
        self._emails: dict[UUID, str | None] = {}
        """The address each user had when it was seen: None for new users."""

    def _track(self, aggregate: AbstractBaseUser) -> None:
        """Remember the user, and the address it was seen with."""
        field = self.model.EMAIL_FIELD
        if field is not None:
            self._emails.setdefault(aggregate.id, getattr(aggregate, field, None))
        super()._track(aggregate)

    async def find_by_identifier(self, value: str) -> AbstractBaseUser | None:
        """The user ``value`` identifies, by the fields of ``AUTH.IDENTIFIERS``.

        The fields are tried in order. The ``USERNAME_FIELD`` matches
        exactly, after NFKC, as before 1.8. A value with ``@`` is an email
        address, in any case, and one in international form a mobile
        number, where those fields identify users; other fields are tried
        only for other values. An address that two users share (rows
        written before 1.8, or outside the repository) identifies neither.
        """
        fields = identifier_fields(self.model)
        username_field, email_field = self.model.USERNAME_FIELD, self.model.EMAIL_FIELD
        number = as_mobile_number(value)
        claimed = ("@" in value and email_field in fields) or (
            number is not None and MOBILE_NUMBER in fields
        )
        for name in fields:
            if name == username_field:  # As before 1.8: exactly, after NFKC.
                user = await self.get_by_natural_key(value)
            elif name == email_field:
                found = await self.find_by_email(value) if "@" in value else []
                user = found[0] if len(found) == 1 else None
            elif name == MOBILE_NUMBER:
                user = None if number is None else await self._find_by(name, number)
            elif claimed:
                continue
            else:
                user = await self._find_by(name, value)
            if user is not None:
                return user
        return None

    async def find_for_staff(self, value: str) -> AbstractBaseUser | None:
        """The user an operator names: by username, then as ``find_by_identifier``.

        A username finds its user, as the commands did before 1.8, even
        where ``AUTH.IDENTIFIERS`` leaves it out; an email address or a
        mobile number finds one where it identifies users.
        """
        user = await self.get_by_natural_key(value)
        return user if user is not None else await self.find_by_identifier(value)

    async def _find_by(self, name: str, value: str) -> AbstractBaseUser | None:
        """The user whose field ``name`` is ``value``, exactly."""
        field = getattr(self.model, name)
        user = await self.session.scalar(select(self.model).where(field == value))
        if user is not None:
            self._track(user)
        return user

    async def refuse_taken_emails(self, lock: Callable[[str], Awaitable[None]]) -> None:
        """Refuse a new or changed address that another user has (ADR 0126).

        Only where email addresses identify users (``AUTH.IDENTIFIERS``).
        ``lock`` takes a lock on the address first, so two transactions
        never both take it: the second waits, then sees the first's user.

        Raises:
            EmailTaken: Another user has the address, in any case.
        """
        field = self.model.EMAIL_FIELD
        if field is None or field not in identifier_fields(self.model):
            return
        table = get_user_table()
        column = table.c[field]
        for user in self.seen:
            email = getattr(user, field, None)
            before = self._emails.get(user.id)
            if not email or (before is not None and same_email(before, email)):
                continue
            await lock(f"domainkit.auth.email:{email_key(email)}")
            statement = select(column).where(
                func.lower(column) == func.lower(email.strip()), table.c.id != user.id
            )
            if any(
                same_email(other, email)
                for other in await self.session.scalars(statement)
            ):
                raise EmailTaken(
                    "Another user has that email address.", fields=(field,)
                )
            self._emails[user.id] = email

    async def get_by_natural_key(self, username: str) -> AbstractBaseUser | None:
        """The user whose ``USERNAME_FIELD`` is ``username`` (normalized)."""
        field = getattr(self.model, self.model.USERNAME_FIELD)
        username = self.model.normalize_username(username)
        user = await self.session.scalar(select(self.model).where(field == username))
        if user is not None:
            self._track(user)
        return user

    async def find_by_mobile_number(self, number: str) -> AbstractBaseUser | None:
        """The user whose mobile number is ``number`` (in international form)."""
        column = get_user_table().c[MOBILE_NUMBER]
        user = await self.session.scalar(select(self.model).where(column == number))
        if user is not None:
            self._track(user)
        return user

    async def mobile_number_taken(self, number: str) -> bool:
        """Whether a user has ``number`` (a model with a ``mobile_number``)."""
        column = get_user_table().c[MOBILE_NUMBER]
        return bool(await self.session.scalar(select(exists().where(column == number))))

    async def username_taken(self, username: str) -> bool:
        """Whether a user has ``username`` (normalized), in any case, or had it.

        A username privacy erased stays taken (``username_erased``).
        """
        name = self.model.normalize_username(username).lower()
        return await self._username_used(name) or await self.username_erased(name)

    async def _username_used(self, username: str) -> bool:
        """Whether a user has ``username`` (normalized), in any case."""
        column = get_user_table().c[self.model.USERNAME_FIELD]
        name = self.model.normalize_username(username).lower()
        return bool(
            await self.session.scalar(
                select(exists().where(func.lower(column) == name))
            )
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
        last passwords, and its codes: secrets of an account nobody may use. Every
        principal of the user
        is read again after the commit. The caller commits.
        """
        await TokenStore(self.session).revoke_user(user_id)
        await MfaStore(self.session).remove(user_id)
        for table in (step_ups, sca_challenges, password_history, auth_codes):
            await self.session.execute(delete(table).where(table.c.user_id == user_id))

    async def _add(self, aggregate: AbstractBaseUser) -> None:
        """Store a new user, unless privacy erased its username from another.

        Raises:
            IdentifierRequired: It has none of the fields of
                ``AUTH.IDENTIFIERS`` (ADR 0126).
            InvalidUsername: The name starts with ``erased-``, which erasure
                keeps for itself (for user models of any kind).
            UsernameTaken: It did: the name stays taken, as a live one is.
        """
        identifiers = identifier_fields(self.model)
        if not any(getattr(aggregate, name, None) for name in identifiers):
            raise IdentifierRequired(
                f"Give one of these, which identify users: {', '.join(identifiers)}."
            )
        field = self.model.USERNAME_FIELD
        username = getattr(aggregate, field, None)
        if username is not None:
            check_not_reserved(str(username))
            if await self.username_erased(str(username)):
                raise UsernameTaken(f"The {field} is already taken.", fields=(field,))
        await super()._add(aggregate)
        self._emails[aggregate.id] = None

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
        username: str | None,
        password: str | None = None,
        **fields: Any,  # noqa: ANN401 - the fields of each user model.
    ) -> AbstractBaseUser:
        """Add a user; without ``password``, it gets an unusable one.

        ``username`` may be None where the user model allows it: the user
        then needs another identifier, such as an email address.

        Passwords are hashed, not validated: call ``validate_password``
        first where people choose them.

        Raises:
            UsernameTaken: Another user has ``username``, in any case, as
                signup and the identity providers refuse it: ``Alice`` and
                ``alice`` would be two accounts that one login throttle
                counts as one. A name privacy erased stays taken too
                (``_add``).
        """
        if username is not None:
            username = self.model.normalize_username(username)
            if await self._username_used(username):
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
        username: str | None,
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
        """Users by username; ``search`` keeps those it finds (``user_search``)."""
        name = F(self.model.USERNAME_FIELD)
        found = user_search(self.model, search) if search else None
        return Selection(found, ordering=[name])

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


def searched_fields(model: type[AbstractBaseUser]) -> tuple[str, ...]:
    """The fields staff find users by: the username, then ``AUTH.IDENTIFIERS``."""
    return tuple(dict.fromkeys((model.USERNAME_FIELD, *identifier_fields(model))))


def user_search(model: type[AbstractBaseUser], search: str) -> Criteria:
    """Users whose username or identifiers contain ``search``, in any case.

    Since 1.8 a user may have no username, and be known by an email
    address or a mobile number alone (``AUTH.IDENTIFIERS``): each of
    ``searched_fields`` is searched. A mobile number also matches however
    ``search`` writes it (``+20 100 123 4567``). An encrypted field matches
    only the whole value, through its blind index, and is skipped without
    one: its ciphertext holds no part of the value.
    """
    columns = get_user_table().c
    number = as_mobile_number(search)
    found: list[Criteria] = []
    for name in searched_fields(model):
        kind = columns[name].type
        if not isinstance(kind, EncryptedType):
            found.append(F(name).icontains(search))
        elif kind.blind_index is not None:
            found.append(F(name) == search)
        else:
            continue
        if name == MOBILE_NUMBER and number is not None:
            found.append(F(name) == number)
    return reduce(or_, found) if found else F("id").in_(())  # None searchable.


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

        The current password counts among them, kept or not. A hash no
        hasher of ``AUTH.PASSWORD_HASHERS`` reads (one removed since) cannot
        be compared: it is skipped, with a security log line, rather than
        refusing every password.

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
            try:
                identify_hasher(hashed)
            except ValueError:
                # Refusing it as a match would refuse every new password,
                # and keep a user who must change one out for good.
                security_logger.warning(
                    "A kept password hash of user %s was made by a hasher no "
                    "longer in AUTH.PASSWORD_HASHERS: reuse of that password is "
                    "not checked",
                    user_id,
                )
                continue
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
