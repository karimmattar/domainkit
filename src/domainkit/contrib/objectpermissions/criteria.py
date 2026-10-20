"""``VisibleTo``: the objects a user may see, as criteria the database checks.

A resource's ``scope`` narrows lists and items to what the caller may
reach. With object permissions, that is every object for users who hold
the permission on the model, and otherwise those granted to them or to
one of their groups::

    products = ResourceRouter(..., scope=visible_scope("view", Product))

In SQL, ``VisibleTo`` is an ``EXISTS`` on the grants table, so pages,
counts, and cursors never see the rest. Credentials limited to scopes see
nothing unless their scopes hold the permission. In memory (``InMemoryRepository``),
it reads the grants it is given: ``visible_scope(..., grants=fake_grants)``.
"""

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Protocol
from uuid import UUID

from sqlalchemy import ColumnElement, and_, exists, false, or_
from starlette.requests import HTTPConnection

from domainkit.contrib.auth.access import current_user, scopes_of
from domainkit.contrib.auth.backends import has_perm
from domainkit.contrib.auth.models import AnyUser
from domainkit.db.query import column_of, mapper_of, register_compiler
from domainkit.ddd.fields import InvalidQuery
from domainkit.ddd.query import Condition, Criteria

from .models import ObjectId, object_column
from .tables import object_permissions


class Grants(Protocol):
    """What ``VisibleTo`` reads in memory (``InMemoryObjectPermissions``)."""

    def allows(
        self,
        permission: str,
        content_type: str,
        object_id: ObjectId,
        user_id: UUID | None,
        group_ids: frozenset[UUID],
    ) -> bool:
        """Whether a grant gives ``permission`` on the object."""
        ...


@dataclass(frozen=True, slots=True)
class VisibleTo(Criteria):
    """Objects on which ``permission`` is granted to a user or their groups.

    ``permission`` is a key (``catalog.view_product``), or an action
    (``view``) that ``bind`` completes with the model's content type.
    """

    permission: str
    user_id: UUID | None
    group_ids: frozenset[UUID] = frozenset()
    grants: Grants | None = field(default=None, compare=False, repr=False)
    content_type: str | None = None
    """The model's content type, once bound."""

    def bind(self, model: type) -> "VisibleTo":
        """Complete the permission, and check it is the model's.

        Raises:
            InvalidQuery: The model is not mapped in an installed domain,
                or the permission is another model's.
        """
        from domainkit.contrib.contenttypes import content_type_key

        try:
            domain, name = content_type_key(model)
        except LookupError as exc:
            raise InvalidQuery(str(exc)) from None
        key = (
            self.permission
            if "." in self.permission
            else f"{domain}.{self.permission}_{name}"
        )
        if not key.startswith(f"{domain}."):
            raise InvalidQuery(f"{key} is not a permission of {domain}.{name}.")
        return replace(self, permission=key, content_type=f"{domain}.{name}")

    def evaluate(self, candidate: object) -> bool:
        """Whether the grants given allow the permission on ``candidate``.

        Raises:
            InvalidQuery: It is not bound, or no grants were given to read.
        """
        if self.content_type is None:
            raise InvalidQuery("Bind the criteria to a class before evaluating them.")
        if self.grants is None:
            raise InvalidQuery(
                "VisibleTo evaluates in memory against the grants it is given "
                "(grants=InMemoryObjectPermissions()); the database checks it in SQL."
            )
        return self.grants.allows(
            self.permission,
            self.content_type,
            getattr(candidate, "id"),  # noqa: B009 - any aggregate's.
            self.user_id,
            self.group_ids,
        )

    def conditions(self) -> Iterator[Condition]:
        """No field conditions: it is about the grants table."""
        return iter(())


async def visible_to(
    user: AnyUser,
    permission: str,
    model: type,
    *,
    grants: Grants | None = None,
    request: HTTPConnection | None = None,
) -> Criteria | None:
    """What ``user`` may see of ``model``: None (everything), or ``VisibleTo``.

    Users who hold the permission on the model see everything; others see
    what is granted to them or their groups; inactive and anonymous users
    see nothing. With the ``request``, so do credentials whose scopes leave
    the permission out, as ``HasPermissions`` would refuse them.
    """
    bound = VisibleTo(permission, None, grants=grants).bind(model)
    if request is not None:
        scopes = scopes_of(request)
        if scopes is not None and bound.permission not in scopes:
            return bound
    if await has_perm(user, bound.permission):
        return None
    if not (user.is_authenticated and user.is_active):
        return bound
    groups = frozenset(group.id for group in getattr(user, "groups", ()))
    return replace(bound, user_id=user.id, group_ids=groups)


def visible_scope(
    permission: str, model: type, *, grants: Grants | None = None
) -> Callable[[HTTPConnection], Awaitable[Criteria | None]]:
    """A resource's ``scope``: what the caller may see by ``permission``."""

    async def scope(request: HTTPConnection) -> Criteria | None:
        return await visible_to(
            current_user(request), permission, model, grants=grants, request=request
        )

    return scope


def compile_visible(
    criteria: VisibleTo, model: type, dialect: str
) -> ColumnElement[bool]:
    """``EXISTS`` a grant on the row's id, to the user or one of their groups."""
    whom: list[ColumnElement[bool]] = []
    grants = object_permissions.c
    if criteria.user_id is not None:
        whom.append(grants.user_id == criteria.user_id)
    if criteria.group_ids:
        whom.append(grants.group_id.in_(sorted(criteria.group_ids)))
    if not whom:
        return false()
    identifier = column_of(mapper_of(model), "id")
    column = object_permissions.c[object_column(identifier.type.python_type)]
    return exists().where(
        and_(
            grants.content_type == criteria.content_type,
            grants.permission == criteria.permission,
            column == identifier,
            or_(*whom),
        )
    )


register_compiler(VisibleTo, compile_visible)
