"""``POST <path>``: one command for each of many items, each answered (``207``).

What each item and the answer hold is ``domainkit.resources.batches``'.
Each item is the command's body, and, for a command on one aggregate, the
id it acts on and an ``if_match`` of its own: it runs as a request of its
own to a command route would, in a context of its own. Without
``atomic``, each item is a use case of its own; with it, all run in one
transaction, and the first failure undoes them all.
"""

import dataclasses
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

from fastapi import Response, status
from pydantic import BaseModel, ConfigDict, Field, create_model

from domainkit.ddd.messages import Command
from domainkit.ddd.uow import UnitOfWork
from domainkit.http.responses import BuiltJSONResponse
from domainkit.utils.text import snake_case

from ..access import ADD, CHANGE, DELETE
from ..batches import (
    NOT_APPLIED,
    BatchAnswer,
    BatchResult,
    Enlisted,
    Undo,
    failure,
    not_applied,
)
from ..schemas import PatchModel
from . import presenting, reach
from .context import BATCH, DEFAULT, MODELS, Returns, RouteContext, RouteSpec, Service
from .declaring import add_route
from .values import changes, check_given, summary_of

if TYPE_CHECKING:
    from domainkit.http.access import Rule

    from . import ResourceRouter
    from .reach import Joined

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

        async def settle(each: BaseModel, work: UnitOfWork) -> Render:
            try:
                return await self.attempt(context, fixed, each, work)
            except Exception as exc:
                refused = failure(exc)
                if refused is None:
                    raise
                return _Settled(refused)

        if not payload.atomic:
            renders = [await settle(each, context.uow) for each in payload.items]
            results = [await render() for render in renders]
        else:
            results = await _atomically(context.uow, payload.items, settle)
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

            result = await reach.on_one(router, own, self.rules, self.objects, run)
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
                    render = await settle(each, lent)
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
    return [
        failed if index == len(renders) - 1 else not_applied()
        for index in range(len(items))
    ]


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
