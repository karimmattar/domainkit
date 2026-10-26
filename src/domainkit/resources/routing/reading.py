"""``GET`` one aggregate, with its ``ETag``; ``If-None-Match`` answers ``304``."""

from typing import TYPE_CHECKING

from fastapi import Response

from domainkit.http.access import check_object_permissions

from ..access import VIEW
from . import presenting, reach
from .context import RouteContext, RouteSpec
from .declaring import add_route

if TYPE_CHECKING:
    from . import ResourceRouter


class ReadRoute:
    """``GET /<prefix>/{<lookup>}`` (see the module)."""

    def __init__(self, router: "ResourceRouter") -> None:
        """Read ``router``'s aggregates one at a time."""
        self.router = router
        self.rules = reach.rules_of(router, VIEW)
        self.objects = reach.asks_objects(self.rules)
        """Whether a rule looks at the aggregate itself."""

    def declare(self) -> None:
        """Add the route to the resource."""
        router = self.router
        add_route(
            router,
            RouteSpec(
                f"/{{{router.lookup}}}",
                "GET",
                f"read_{router.basename}",
                f"Read a {router.basename.replace('_', ' ')}",
                VIEW,
                detail=True,
                reads=True,
                returns=router.schema,
            ),
            self.handle,
        )

    async def handle(self, context: RouteContext) -> Response:
        """The aggregate, if the caller may reach it and its rules allow."""
        router = self.router
        request = context.request
        async with context.uow as uow:
            aggregate = await reach.load(router, uow, request, context.ids)
        if self.objects:
            await check_object_permissions(request, aggregate, *self.rules)
        return await presenting.respond(
            router, context, aggregate, if_none_match=context.if_none_match
        )
