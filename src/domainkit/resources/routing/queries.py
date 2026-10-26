"""A ``GET`` route that answers a ``Query`` message through its handler.

The query's fields come from the query string, bounded as
``schema_for(query, write=True)`` describes them, and the ids from the
path: a query with the resource's ``lookup`` field asks about one
aggregate, at ``/{lookup}<path>``. The handler answers an aggregate, sent
as a read sends it, with its ``ETag``; or, as its return annotation says,
a sequence of them, sent as ``{"items": [...]}``. Either way, what the
caller may not reach (``scope``, ``parent``) or see (``policy``) is left out.
"""

from collections.abc import Iterable
from typing import TYPE_CHECKING, cast

from fastapi import Response
from pydantic import BaseModel, create_model

from domainkit.ddd.fields import fields_of
from domainkit.ddd.messages import Query as QueryMessage
from domainkit.ddd.model import AggregateRoot
from domainkit.ddd.query import as_criteria
from domainkit.http.responses import BuiltJSONResponse
from domainkit.utils.text import snake_case

from ..schemas import schema_for
from . import presenting, reach
from .context import MODELS, Aggregate, Handler, RouteContext, RouteSpec
from .declaring import add_route
from .values import label_of, returns_many, summary_of, to_domain

if TYPE_CHECKING:
    from domainkit.ddd.uow import UnitOfWork
    from domainkit.http.access import Rule

    from . import ResourceRouter
    from .reach import Joined


class QueryRoute:
    """``ResourceRouter.query_route`` (see the module)."""

    def __init__(
        self,
        router: "ResourceRouter",
        query: type[QueryMessage],
        *,
        handler: Handler,
        path: str,
        action: str,
        rule: "Rule | None",
        name: str | None,
        summary: str | None,
    ) -> None:
        """Check the path and the handler.

        Raises:
            TypeError: The path is empty, or does not start with ``/``; the
                handler does not annotate what it returns.
        """
        if not path.startswith("/"):
            raise TypeError(f"A query route's path starts with /, not {path!r}.")
        self.router = router
        self.query = query
        self.handler = handler
        self.many = returns_many(handler)
        self.fields = fields_of(query)
        self.detail = router.lookup in self.fields
        self.from_path = {router.lookup} if self.detail else set()
        if router.parent_field is not None and router.parent_field in self.fields:
            self.from_path.add(router.parent_field)
        asked = (
            schema_for(query, write=True, exclude=sorted(self.from_path))
            if set(self.fields) - self.from_path
            else None
        )
        self.rules = reach.rules_of(router, action, rule)
        self.objects = reach.asks_objects(self.rules)
        """Whether a rule looks at the aggregate itself."""
        self.spec = RouteSpec(
            f"/{{{router.lookup}}}{path}" if self.detail else path,
            "GET",
            name or snake_case(query.__name__),
            summary or summary_of(query),
            action,
            rule=rule,
            detail=self.detail,
            reads=not self.many,
            asked=asked,
            returns=_answer(query, router.schema) if self.many else router.schema,
        )

    def declare(self) -> None:
        """Add the route to the resource; one on the collection before one item's."""
        router = self.router
        add_route(router, self.spec, self.handle)
        if not self.detail:
            router.before_items(router.routes.pop())

    async def handle(self, context: RouteContext) -> Response:
        """Ask the handler; answer what the caller may reach and see of it."""
        router = self.router
        request = context.request
        given = context.parameters
        arguments = {
            name: to_domain(self.fields[name], value)
            for name, value in (given.model_dump() if given else {}).items()
        }
        arguments |= {
            name: context.ids[reach.path_name(router, name)] for name in self.from_path
        }

        async def run(uow: "UnitOfWork | Joined") -> object:
            return await self.handler(self.query(**arguments), uow)

        result = (
            await reach.on_one(router, context, self.rules, self.objects, run)
            if self.detail
            else await run(context.uow)
        )
        criteria = [
            as_criteria(each)
            for each in await reach.criteria(router, request, context.ids)
        ]
        if self.many:
            hidden = await presenting.hidden_fields(router, request)
            items = [
                presenting.dump(router, context, item, hidden)
                for item in cast("Iterable[Aggregate]", result)
                if all(each.matches(item) for each in criteria)
                and await reach.allows(request, item, self.rules)
            ]
            return BuiltJSONResponse({"items": items})
        if not isinstance(result, AggregateRoot):
            raise RuntimeError(
                f"{label_of(self.handler)} answered {type(result).__name__}, not "
                f"a {router.model.__name__}."
            )
        if not all(each.matches(result) for each in criteria):
            missing = router.missing or router.repository(context.uow).not_found
            raise missing(result.id)
        await reach.check_object(request, result, self.rules)
        return await presenting.respond(
            router, context, result, if_none_match=context.if_none_match
        )


def _answer(query: type, schema: type[BaseModel]) -> type[BaseModel]:
    """``<Query>Answer``: the items a query that answers many returns."""
    return create_model(
        f"{query.__name__}Answer",
        __module__=MODELS,
        items=(list[schema], ...),  # type: ignore[valid-type]
    )
