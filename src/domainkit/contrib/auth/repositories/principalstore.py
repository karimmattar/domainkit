"""What a request's credentials name, read in one statement."""

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
    exists,
    func,
    literal,
    null,
    select,
    true,
    type_coerce,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlalchemy.sql.elements import ColumnElement

from domainkit.contrib.contenttypes.tables import content_types

from ..models import UNUSABLE_PASSWORD_PREFIX, AbstractBaseUser
from ..principals import (
    PASSWORD,
    Credential,
    GrantedPermission,
    Login,
    Membership,
    Principal,
    Snapshot,
    shared,
    user_shape,
    withhold_password,
)
from ..tables import group_permissions, groups, permissions, tokens, user_link_tables
from ..users import get_user_model, get_user_table

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
