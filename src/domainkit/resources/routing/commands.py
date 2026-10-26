"""A route that runs a command through an application service.

A command with the resource's ``lookup`` field changes one aggregate: its
route is ``/{lookup}<path>``, it is held to ``If-Match``, and the caller
must reach the aggregate first. Others act on the collection. The command
is built from the body, the ids in the path, and the ``provide``
dependencies, and answered as ``returns`` says.
"""

import dataclasses
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Literal

from fastapi import Response, status
from pydantic import BaseModel

from domainkit.ddd.messages import Command
from domainkit.utils.text import snake_case

from ..access import ADD, CHANGE, DELETE
from ..schemas import PatchModel
from . import presenting, reach
from .context import COMMAND, DEFAULT, Returns, RouteContext, RouteSpec, Service
from .declaring import add_route
from .values import changes, check_given, summary_of

if TYPE_CHECKING:
    from domainkit.ddd.uow import UnitOfWork
    from domainkit.http.access import Rule

    from . import ResourceRouter
    from .reach import Joined


class CommandRoute:
    """``ResourceRouter.command_route`` (see the module)."""

    def __init__(
        self,
        router: "ResourceRouter",
        command: type[Command],
        *,
        service: Service,
        body: type[BaseModel] | None,
        returns: Returns,
        method: Literal["POST", "PUT", "PATCH", "DELETE"],
        path: str,
        action: str | None,
        rule: "Rule | None",
        status_code: int | None,
        name: str | None,
        summary: str | None,
        provide: Mapping[str, Callable[..., object]] | None,
        body_limit: int | None = None,
    ) -> None:
        """Check the command against what gives its fields.

        Raises:
            TypeError: The body has fields the command lacks, or the
                command needs fields nothing gives.
        """
        names = {field.name for field in dataclasses.fields(command)}
        self.router = router
        self.command = command
        self.service = service
        self.detail = router.lookup in names
        self.from_path = {router.lookup} if self.detail else set()
        if router.parent_field is not None and router.parent_field in names:
            self.from_path.add(router.parent_field)
        self.provided = dict(provide or {})
        given = set(body.model_fields) if body is not None else set()
        check_given(command, given, self.provided, self.from_path)
        self.returns, self.status_code, self.action = _answers(
            method, self.detail, returns, status_code, action
        )
        self.partial = body is not None and issubclass(body, PatchModel)
        if self.partial and router.policy is not None:
            body = router.policy.describe_changes(body)  # type: ignore[arg-type]
        self.body = body
        self.rules = reach.rules_of(router, self.action, rule)
        self.objects = reach.asks_objects(self.rules)
        """Whether a rule looks at the aggregate itself."""
        self.spec = RouteSpec(
            f"/{{{router.lookup}}}{path}" if self.detail else path,
            method,
            name or snake_case(command.__name__),
            summary or summary_of(command),
            self.action,
            rule=rule,
            detail=self.detail,
            body=body,
            returns=self.returns,
            status_code=self.status_code,
            writes=True,
            provided=self.provided,
            body_limit=router.body_limit_of(COMMAND, body_limit),
        )

    def declare(self) -> None:
        """Add the route to the resource."""
        add_route(self.router, self.spec, self.handle)

    async def handle(self, context: RouteContext) -> Response:
        """Build the command, run it, and answer with what the service returned."""
        router = self.router
        request = context.request
        given = changes(context.body)
        if self.partial and router.policy is not None:
            await router.policy.check_changes(request, given)
        built = self.command(
            **{
                **given,
                **context.provided,
                **{
                    name: context.ids[reach.path_name(router, name)]
                    for name in self.from_path
                },
            }
        )
        if not self.detail:
            result = await self.service(built, context.uow)
        else:

            async def run(uow: "UnitOfWork | Joined") -> object:
                return await reach.conditionally(
                    router, context, lambda: self.service(built, uow)
                )

            result = await reach.on_one(router, context, self.rules, self.objects, run)
        return await presenting.answer(
            router, context, result, self.returns, self.status_code
        )


def _answers(
    method: str,
    detail: bool,
    returns: Returns,
    status_code: int | None,
    action: str | None,
) -> tuple[Returns, int, str]:
    """What a command route answers with, its status, and the action it checks.

    A ``DELETE`` answers nothing by default (``204``); a ``POST`` on the
    collection creates (``201``, ``add``); others change (``200``).
    """
    if returns is DEFAULT and method == "DELETE":
        returns = None
    creates = method == "POST" and not detail
    code = status_code or (
        status.HTTP_204_NO_CONTENT
        if returns is None
        else status.HTTP_201_CREATED
        if creates
        else status.HTTP_200_OK
    )
    verb = action or (DELETE if method == "DELETE" else ADD if creates else CHANGE)
    return returns, code, verb
