"""``POST <path>``: one command for each of many items, each answered (``207``).

What each item and the answer hold is ``domainkit.resources.batches``'.
Each item is the command's body, and, for a command on one aggregate, the
id it acts on and an ``if_match`` of its own: it runs as a request of its
own to a command route would, in a context of its own. Without
``atomic``, each item is a use case of its own; with it, all run in one
transaction, and the first failure undoes them all.

A batch route that declares ``retry`` runs work that lost a race again
(``UnitOfWork.run``): without ``atomic``, the item that lost, its reach
check and ``If-Match`` included; with it, the whole batch, in a new
transaction, since the race ended the one it ran in. Once the retries are
spent, the batch answers as one that does not retry.
"""

import dataclasses
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast

from fastapi import Response, status
from pydantic import BaseModel, ConfigDict, Field, create_model

from domainkit.context import UNMATCHED_PATH, current_request
from domainkit.ddd.messages import Command
from domainkit.ddd.uow import UnitOfWork
from domainkit.exceptions import DomainkitError
from domainkit.http.access import check_object_permissions
from domainkit.http.responses import BuiltJSONResponse
from domainkit.reporting import report_error
from domainkit.utils.text import snake_case

from ..access import ADD, CHANGE, DELETE
from ..batches import (
    NOT_APPLIED,
    BatchAnswer,
    BatchResult,
    Enlisted,
    Undo,
    crashed,
    failure,
    not_applied,
)
from ..schemas import PatchModel
from . import presenting, reach
from .context import BATCH, DEFAULT, MODELS, Returns, RouteContext, RouteSpec, Service
from .declaring import add_route
from .values import changes, check_given, summary_of

if TYPE_CHECKING:
    from domainkit.ddd.uow import Retry
    from domainkit.http.access import Rule

    from . import ResourceRouter
    from .reach import Joined

logger = logging.getLogger(__name__)

type Render = Callable[[], Awaitable[BatchResult]]
"""How an item that ran is answered, once the batch's outcome is known."""


class _Batch(Protocol):
    """A batch's body, as ``batch_route`` makes its model."""

    items: list[BaseModel]
    atomic: bool


class BatchRoute:
    """``ResourceRouter.batch_route`` (see the module)."""

    def __init__(
        self,
        router: "ResourceRouter",
        command: type[Command],
        *,
        service: Service,
        body: type[BaseModel] | None,
        path: str,
        action: str | None,
        rule: "Rule | None",
        returns: Returns,
        max_items: int,
        name: str | None,
        summary: str | None,
        provide: Mapping[str, Callable[..., object]] | None,
        body_limit: int | None = None,
        retry: "Retry | None" = None,
    ) -> None:
        """Check the command against what gives its fields.

        Raises:
            TypeError: The path, the bound, or the body does not fit the command.
        """
        if not path.startswith("/") or max_items < 1:
            raise TypeError(
                "A batch's path starts with /, and it takes 1 item or more."
            )
        if body is not None and issubclass(body, PatchModel):
            raise TypeError("Batches take whole bodies, not partial changes.")
        names = {field.name for field in dataclasses.fields(command)}
        lookup = router.lookup
        self.router = router
        self.command = command
        self.service = service
        self.retry = retry
        """Run what lost a race again: an item, or an atomic batch (``run``)."""
        self.detail = lookup in names
        self.from_path = (
            {router.parent_field}
            if router.parent_field is not None and router.parent_field in names
            else set()
        )
        given = set(body.model_fields) if body is not None else set()
        if self.detail and given & {lookup, "if_match"}:
            raise TypeError(
                f"Items name their aggregate by {lookup}; leave it, and "
                "if_match, out of the body."
            )
        self.provided = dict(provide or {})
        check_given(command, given, self.provided, self.from_path | ({lookup} & names))
        verb = action or (CHANGE if self.detail else ADD)
        if returns is DEFAULT and verb == DELETE:
            returns = None
        self.returns = returns
        self.status_code = (
            status.HTTP_200_OK if self.detail else status.HTTP_201_CREATED
        )
        self.rules = reach.rules_of(router, verb, rule)
        self.objects = reach.asks_objects(self.rules)
        """Whether a rule looks at the aggregate itself."""
        item = _item(command, body, lookup, router.id_type, self.detail)
        batch = create_model(
            f"{command.__name__}Batch",
            __module__=MODELS,
            __config__=ConfigDict(extra="forbid"),
            items=(list[item], Field(min_length=1, max_length=max_items)),  # type: ignore[valid-type]
            atomic=(
                bool,
                Field(default=False, description="All items, or none of them."),
            ),
        )
        self.spec = RouteSpec(
            path,
            "POST",
            name or f"{snake_case(command.__name__)}_batch",
            summary or f"{summary_of(command) or command.__name__}, for many",
            verb,
            rule=rule,
            body=batch,
            returns=BatchAnswer,
            status_code=status.HTTP_207_MULTI_STATUS,
            writes=True,
            provided=self.provided,
            body_limit=router.body_limit_of(BATCH, body_limit),
        )

    def declare(self) -> None:
        """Add the route to the resource, before the routes of one item."""
        router = self.router
        add_route(router, self.spec, self.handle)
        router.before_items(router.routes.pop())

    async def handle(self, context: RouteContext) -> Response:
        """Run every item; answer each one's result, in order."""
        router = self.router
        fixed = {
            **context.provided,
            **{
                name: context.ids[reach.path_name(router, name)]
                for name in self.from_path
            },
        }
        payload = cast(_Batch, context.body)
        retry = self.retry
        uow = context.uow

        async def settle(each: BaseModel, work: UnitOfWork) -> Render:
            try:
                if retry is None or payload.atomic:
                    return await self.attempt(context, fixed, each, work)
                run = partial(self.attempt, context, fixed, each, work)
                return await work.run(run, retry=retry)
            except Exception as exc:
                if retry is not None and payload.atomic and _lost(uow, retry, exc):
                    raise _LostRace(exc) from exc  # The whole batch runs again.
                refused = failure(exc)
                if refused is None:
                    raise
                return _Settled(refused)

        if not payload.atomic:
            renders = await _one_by_one(context, payload.items, settle)
            results = [await render() for render in renders]
        elif refused_first := await self.check_objects(context, payload.items):
            results = refused_first
        elif retry is None:
            results = await _atomically(uow, payload.items, settle)
        else:
            whole = dataclasses.replace(retry, retry_on=(*retry.retry_on, _LostRace))
            try:
                results = await uow.run(
                    partial(_atomically, uow, payload.items, settle), retry=whole
                )
            except _LostRace as lost:
                refused = failure(lost.exc)
                if refused is None:  # Not a refusal: raised, as without retry.
                    for note in getattr(lost, "__notes__", ()):
                        lost.exc.add_note(note)
                    raise lost.exc from None
                results = _undone(len(payload.items), lost.at, refused)
        answer = BatchAnswer(
            results=results,
            failed=sum(
                1
                for each in results
                if each.error is not None and each.error.code != NOT_APPLIED
            ),
        )
        return BuiltJSONResponse(
            answer.model_dump(exclude_none=True),
            status_code=status.HTTP_207_MULTI_STATUS,
        )

    async def check_objects(
        self, context: RouteContext, items: Sequence[BaseModel]
    ) -> list[BatchResult] | None:
        """Ask the object rules about each item's aggregate, before an atomic batch.

        Each aggregate is read in a unit of work of its own, closed before
        the rules run, as for a command (``reach.on_one``): a rule may read
        the database itself (object permission grants), and must not wait
        for a connection while the batch's transaction holds one (WEB-16).
        The answer of the first item refused, the rest undone; None when
        every item may go on, or no rule looks at objects.

        Raises:
            Exception: What reading an aggregate or a rule raised, if it is
                no refusal (a bug).
        """
        router = self.router
        if not (self.detail and self.objects):
            return None
        for at, each in enumerate(items):
            ids = {**context.ids, router.lookup: getattr(each, router.lookup)}
            try:
                async with context.uow as uow:
                    aggregate = await reach.load(router, uow, context.request, ids)
                await check_object_permissions(context.request, aggregate, *self.rules)
            except Exception as exc:
                refused = failure(exc)
                if refused is None:
                    raise
                return _undone(len(items), at, refused)
        return None

    async def attempt(
        self,
        context: RouteContext,
        fixed: Mapping[str, object],
        each: BaseModel,
        work: UnitOfWork,
    ) -> Render:
        """Run one item in ``work``; how to answer it, once the batch is done."""
        router = self.router
        arguments = changes(each)
        id_ = arguments.pop(router.lookup, None)
        own = dataclasses.replace(
            context,
            uow=work,
            ids={**context.ids, router.lookup: id_},
            if_match=arguments.pop("if_match", None),
        )
        built = self.command(
            **{**arguments, **fixed, **({router.lookup: id_} if self.detail else {})}
        )
        if self.detail:

            async def run(uow: "UnitOfWork | Joined") -> object:
                return await reach.conditionally(
                    router, own, lambda: self.service(built, uow)
                )

            # An atomic batch asked the object rules before its transaction.
            objects = self.objects and not isinstance(work, Enlisted)
            result = await reach.on_one(router, own, self.rules, objects, run)
        else:
            result = await self.service(built, work)
        return lambda: presenting.batch_result(
            router, own, result, self.returns, self.status_code
        )


async def _atomically(
    uow: UnitOfWork,
    items: Sequence[BaseModel],
    settle: Callable[[BaseModel, UnitOfWork], Awaitable[Render]],
) -> list[BatchResult]:
    """Run every item in one transaction; the first failure undoes them all."""
    renders: list[Render] = []
    failed: BatchResult | None = None
    async with uow:
        try:
            async with uow.savepoint():
                lent = cast(UnitOfWork, Enlisted(uow))
                for each in items:
                    try:
                        render = await settle(each, lent)
                    except _LostRace as lost:
                        lost.at = len(renders)
                        raise
                    renders.append(render)
                    if isinstance(render, _Settled):
                        failed = render.result
                        raise Undo
        except Undo:
            pass
        if failed is None:
            await uow.commit()
    if failed is None:
        return [await render() for render in renders]
    return _undone(len(items), len(renders) - 1, failed)


async def _one_by_one(
    context: RouteContext,
    items: Sequence[BaseModel],
    settle: Callable[[BaseModel, UnitOfWork], Awaitable[Render]],
) -> list[Render]:
    """Run each item as a use case of its own, in order; stop at an unexpected error.

    An unexpected error of the first item fails the request, as a request
    of its own would fail. Past it, the items before it may have committed,
    so the batch still answers each one (WEB-17): the item that raised is a
    500, logged and reported as the request's error would be, and the rest
    are not run (``424``).
    """
    renders: list[Render] = []
    for at, each in enumerate(items):
        try:
            renders.append(await settle(each, context.uow))
        except Exception as exc:
            if not renders:
                raise
            _report(context, exc)
            renders.append(_Settled(crashed()))
            renders.extend(_Settled(not_applied()) for _ in items[at + 1 :])
            break
    return renders


def _report(context: RouteContext, exc: Exception) -> None:
    """Log and report an item's error, as ``RequestContextMiddleware`` would."""
    request = current_request()
    method = context.request.scope.get("method", "POST")
    path = request.path if request is not None else UNMATCHED_PATH
    logger.error("Unhandled error in a batch item of %s %s", method, path, exc_info=exc)
    report_error(
        exc,
        request_id=request.request_id if request is not None else None,
        method=method,
        path=path,
    )


def _undone(count: int, at: int, failed: BatchResult) -> list[BatchResult]:
    """The answer of an atomic batch whose item ``at`` failed: the rest undone."""
    return [failed if index == at else not_applied() for index in range(count)]


def _lost(uow: UnitOfWork, retry: "Retry", exc: Exception) -> bool:
    """Whether ``exc`` is a race lost that ``retry`` runs work again after."""
    return isinstance(exc, retry.retry_on) or uow._conflicted(exc)


class _LostRace(DomainkitError):
    """An item of an atomic batch lost a race: the batch runs again, whole."""

    def __init__(self, exc: Exception) -> None:
        """Carry the item's failure; ``at`` is set where it is known."""
        super().__init__(str(exc))
        self.exc = exc
        """What the item raised, answered if the batch is not run again."""
        self.at = 0
        """The index of the item that lost."""


@dataclasses.dataclass(frozen=True, slots=True)
class _Settled:
    """An item's result known already: it failed, so there is nothing to render."""

    result: BatchResult

    async def __call__(self) -> BatchResult:
        """The result."""
        return self.result


def _item(
    command: type[Command],
    body: type[BaseModel] | None,
    lookup: str,
    id_type: type,
    detail: bool,
) -> type[BaseModel]:
    """``<Command>Item``: the body's fields, and the aggregate's id and If-Match."""
    fields: dict[str, Any] = {}
    if detail:
        fields[lookup] = (id_type, Field(description="The aggregate it acts on."))
        fields["if_match"] = (
            str | None,
            Field(
                default=None,
                max_length=4096,
                description="The ETag of the version it changes; 412 if it changed.",
            ),
        )
    if body is None:
        return create_model(
            f"{command.__name__}Item",
            __module__=MODELS,
            __config__=ConfigDict(extra="forbid"),
            **fields,
        )
    return create_model(
        f"{command.__name__}Item", __module__=MODELS, __base__=body, **fields
    )
