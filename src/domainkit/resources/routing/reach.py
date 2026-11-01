"""What a caller may reach of a resource, and do to it.

A caller reaches the aggregates within the resource's ``scope`` and, for a
nested resource, the children of a parent it may reach itself; the rest is
``404``, in lists and items alike. A route's rules may look at the
aggregate itself (``has_object_permission``), and ``If-Match`` holds a
write to the versions it names.

A command or a query on one aggregate is checked before it runs. Where
only the scope or the parent decide, the check and the work share one unit
of work: the aggregate is read within reach, then the service's first
block runs in the same transaction (``Joined``), so nothing changes between
the two; its later blocks get transactions of their own, as ever. Where a
rule looks at the object, the check has a unit of work of its own, closed
before the rule runs: a rule may read the database itself (object
permission grants), and must not wait for a connection while this request
holds one.
"""

from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self

from starlette.requests import HTTPConnection

from domainkit.ddd.exceptions import (
    ConcurrentUpdate,
    PermissionDenied,
    PreconditionFailed,
)
from domainkit.ddd.query import CriteriaLike, F, Selection
from domainkit.ddd.repository import Repository
from domainkit.ddd.uow import UnitOfWork
from domainkit.http.access import (
    AllOf,
    AnyOf,
    BasePermission,
    Not,
    NotAuthenticated,
    Rule,
    check_object_permissions,
)

from ..access import VIEW, rules_for
from ..conditional import PreconditionRequired, expected_versions
from .context import Aggregate, RouteContext

if TYPE_CHECKING:
    from . import ResourceRouter


class Joined:
    """An open unit of work, lent to a service for its first block.

    The route entered the unit of work to check the aggregate, so the
    service's first ``async with uow:`` is that same transaction: entering
    does nothing, and the check's reads are there. Every exit is the unit of
    work's own: it closes the session, so what the block did not commit is
    rolled back, as it always was. A later block enters the unit of work
    again, in a transaction of its own, as without a scope: a change a
    service abandoned in one block never commits with the next.
    """

    def __init__(self, uow: UnitOfWork) -> None:
        """Lend ``uow``, entered already."""
        self._uow = uow
        self._entered = True
        """Whether the unit of work is open for the service's next block."""

    async def __aenter__(self) -> Self:
        """The first block: the open unit of work; any later one: a new one."""
        if self._entered:
            self._entered = False
        else:
            await self._uow.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Leave the block as the unit of work does: what it did not commit goes."""
        self._entered = False
        await self._uow.__aexit__(exc_type, exc, traceback)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - the unit of work's.
        """Commit, repositories, and the rest come from the unit of work."""
        return getattr(self._uow, name)


def path_name(router: "ResourceRouter", name: str) -> str:
    """The path parameter that gives the field ``name`` (the parent's id, by its)."""
    if name == router.parent_field and router.parent is not None:
        return router.parent.lookup
    return name


def rules_of(
    router: "ResourceRouter", action: str, rule: Rule | None = None
) -> tuple[Rule, ...]:
    """What a route of ``action`` checks: the resource's rules, then its own."""
    return (*rules_for(router.rule, action), *([rule] if rule is not None else []))


async def criteria(
    router: "ResourceRouter", request: HTTPConnection, ids: Mapping[str, object]
) -> list[CriteriaLike]:
    """What a caller may reach here: the parent's children, within its scope."""
    found: list[CriteriaLike] = []
    if router.parent is not None and router.parent_field is not None:
        found.append(F(router.parent_field) == ids[router.parent.lookup])
    if router.scope is not None:
        scoped = await router.scope(request)
        if scoped is not None:
            found.append(scoped)
    return found


async def load(
    router: "ResourceRouter",
    uow: UnitOfWork,
    request: HTTPConnection,
    ids: Mapping[str, object],
) -> Aggregate:
    """The aggregate the path names, if the caller may reach it.

    Raises:
        NotFound: There is none, or it is out of the caller's reach
            (the repository's ``not_found``).
    """
    repository: Repository[Aggregate, Any] = router.repository(uow)
    id_ = ids[router.lookup]
    within = await criteria(router, request, ids)
    if not within:
        found = await repository.find(id_)
    else:
        found = await repository.first(Selection(F("id") == id_).where(*within))
    if found is None:
        missing = router.missing or repository.not_found
        raise missing(id_)
    return found


async def check_parent(router: "ResourceRouter", context: RouteContext) -> None:
    """Refuse a parent the caller may not reach, as its own read would.

    Raises:
        NotFound: There is no such parent, or it is out of reach.
    """
    parent = router.parent
    assert parent is not None  # noqa: S101 - called for nested resources only.
    assert context.parent_uow is not None  # noqa: S101 - nested routes take one.
    async with context.parent_uow as uow:
        aggregate = await load(parent, uow, context.request, context.ids)
    await check_object(context.request, aggregate, rules_of(parent, VIEW))


async def check_object(
    request: HTTPConnection, aggregate: object, rules: Sequence[Rule]
) -> None:
    """Ask a route's rules about the aggregate itself, if one looks at objects.

    Raises:
        NotAuthenticated: An anonymous caller was refused.
        PermissionDenied: A rule refuses the caller this aggregate.
    """
    if asks_objects(rules):
        await check_object_permissions(request, aggregate, *rules)


async def allows(
    request: HTTPConnection, aggregate: object, rules: Sequence[Rule]
) -> bool:
    """Whether a route's rules let the caller have the aggregate."""
    try:
        await check_object(request, aggregate, rules)
    except (NotAuthenticated, PermissionDenied):
        return False
    return True


async def on_one[T](
    router: "ResourceRouter",
    context: RouteContext,
    rules: Sequence[Rule],
    objects: bool,
    run: Callable[[UnitOfWork | Joined], Awaitable[T]],
) -> T:
    """``run`` the work on the aggregate the path names, once the caller may.

    ``objects`` says whether a rule looks at objects. Without a scope, a
    parent, or such a rule there is nothing to check. Otherwise the
    aggregate is read within reach first, and ``run`` is given the unit of
    work to do it in: the same, open (see the module), unless a rule had to
    look at the aggregate between. Then ``run`` is held to the version the
    rule saw (``pinned``): one changed meanwhile is 409, never written past
    the rule (SEC-16a-1).

    Raises:
        ConcurrentUpdate: The aggregate changed after a rule looked at it.
        NotFound: The aggregate is out of the caller's reach.
        NotAuthenticated: An anonymous caller was refused the aggregate.
        PermissionDenied: A rule refuses the caller the aggregate.
    """
    uow = context.uow
    if objects:
        async with uow:
            aggregate = await load(router, uow, context.request, context.ids)
        await check_object_permissions(context.request, aggregate, *rules)
        with pinned(router, context, aggregate.version):
            return await run(uow)
    if router.parent is None and router.scope is None:
        return await run(uow)
    async with uow:
        await load(router, uow, context.request, context.ids)
        return await run(Joined(uow))


CHANGED_SINCE_CHECKED: Final = (
    "This record changed while it was being checked; read it again and retry."
)
"""The detail of a write refused because its aggregate moved since a rule saw it."""


@contextmanager
def pinned(
    router: "ResourceRouter", context: RouteContext, version: int | None
) -> Iterator[None]:
    """Hold the aggregate the path names to ``version``, the one a rule saw.

    A rule that looks at objects is asked outside the write's transaction
    (WEB-16), so the write holds the aggregate to the version it was asked
    about: one another request changed in between (given to another owner,
    frozen) answers 409 ``concurrent_update``, as a generic write does
    (SEC-16a-1). A service that never loads it through its repository
    writes nothing of it, and is let be. With ``If-Match`` the request
    named a version itself: a change is its 412. ``version`` None holds
    nothing.

    Raises:
        ConcurrentUpdate: The aggregate is no longer at ``version``.
    """
    if version is None:
        yield
        return
    uow = context.uow
    try:
        with uow.expect(router.model, context.ids[router.lookup], {version}) as held:
            held.checked = True  # Not loaded, not written: nothing to refuse.
            yield
    except PreconditionFailed as exc:
        if context.if_match is not None:
            raise
        raise ConcurrentUpdate(CHANGED_SINCE_CHECKED) from exc


async def keep_in_reach(
    router: "ResourceRouter",
    uow: UnitOfWork,
    request: HTTPConnection,
    ids: Mapping[str, object],
    id_: object,
) -> None:
    """Before a generic write commits: refuse one that leaves the caller's reach.

    The aggregate is read again, as written, with the caller's scope.

    Raises:
        PermissionDenied: The caller could not reach it afterwards.
    """
    within = await criteria(router, request, ids)
    if not within:
        return
    query = Selection(F("id") == id_).where(*within)
    if await router.repository(uow).first(query) is None:
        raise PermissionDenied(
            "You cannot create or move a record outside what you may reach."
        )


async def conditionally[T](
    router: "ResourceRouter",
    context: RouteContext,
    change: Callable[[], Awaitable[T]],
) -> T:
    """Run ``change``, holding the aggregate to the request's ``If-Match``.

    Raises:
        PreconditionRequired: The resource requires ``If-Match``, and the
            request sent none, or ``*``, which names no version.
        PreconditionFailed: The aggregate is not at a version it names.
        RuntimeError: The service never loaded the aggregate, so
            ``If-Match`` could not be checked.
    """
    header = context.if_match
    versions = None if header is None else expected_versions(header)
    if versions is None and router.require_if_match:
        # ``*`` names no version: under the requirement it is no If-Match (WEB-22).
        raise PreconditionRequired
    if header is None:
        return await change()
    id_ = context.ids[router.lookup]
    model = router.model
    with context.uow.expect(model, id_, versions) as expected:
        result = await change()
    if not expected.checked:
        raise RuntimeError(
            f"The service never loaded {model.__name__} {id_} through its "
            "repository, so If-Match could not be checked."
        )
    return result


def asks_objects(rules: Sequence[Rule]) -> bool:
    """Whether any of ``rules`` looks at objects."""
    return any(checks_objects(each) for each in rules)


def checks_objects(rule: Rule) -> bool:
    """Whether ``rule`` (or a rule inside it) looks at objects."""
    if isinstance(rule, type):
        rule = rule()
    if isinstance(rule, AllOf | AnyOf):
        return any(checks_objects(each) for each in rule.rules)
    if isinstance(rule, Not):
        return checks_objects(rule.rule)
    return type(rule).has_object_permission is not BasePermission.has_object_permission
