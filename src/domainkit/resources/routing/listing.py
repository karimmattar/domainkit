"""``GET`` the collection: one page, filtered, or every match exported.

The page is what the resource's ``Pagination`` asks for, filtered by its
``filters``, within the caller's reach; each item is shown as the caller
may see it, with only the ``list_fields``. A resource with ``exports``
answers ``text/csv`` or ``application/x-ndjson`` by ``Accept``: every match,
up to ``max_export``, streamed in chunks.
"""

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Final

from fastapi import Response
from fastapi.responses import StreamingResponse

from domainkit.ddd.query import Query, as_query
from domainkit.http.filters import FilterSet
from domainkit.http.responses import BuiltJSONResponse

from ..access import VIEW
from ..renderers import CSV, JSON, ExportTooLarge, csv_rows, ndjson_lines, negotiate
from . import presenting, reach
from .context import RouteContext, RouteSpec
from .declaring import add_route

if TYPE_CHECKING:
    from . import ResourceRouter

EXPORT_CHUNK: Final = 500
"""How many aggregates an export reads per query."""


class ListRoute:
    """``GET`` the collection (see the module)."""

    def __init__(self, router: "ResourceRouter") -> None:
        """List ``router``'s aggregates."""
        self.router = router

    def declare(self) -> None:
        """Add the route to the resource."""
        router = self.router
        add_route(
            router,
            RouteSpec(
                "",
                "GET",
                f"list_{router.plural}",
                f"List {router.plural.replace('_', ' ')}",
                VIEW,
                listing=True,
                returns=router.page_schema,
            ),
            self.handle,
        )

    async def handle(self, context: RouteContext) -> Response:
        """One page as JSON, or every match in the media type asked for."""
        router = self.router
        request = context.request
        page = context.page
        assert page is not None  # noqa: S101 - listings take a page.
        exporting = len(router.media_types) > 1
        media = (
            negotiate(request.headers.get("accept"), router.media_types)
            if exporting
            else JSON
        )
        query = await self.query(context)
        if media != JSON:
            return await self.export(context, query, media)
        async with context.uow as uow:
            found = await page.fetch(router.repository(uow), query)
        hidden = await presenting.hidden_fields(router, request)
        items = [
            presenting.dump(router, context, item, hidden, router.list_fields)
            for item in found.items
        ]
        out = page.respond(found, items, query)
        body = {"items": items}
        for name in router.page_schema.model_fields:
            if name != "items":
                body[name] = getattr(out, name)
        return BuiltJSONResponse(
            body, headers={"Vary": "Accept"} if exporting else None
        )

    async def query(self, context: RouteContext) -> Query:
        """The listing's query: the filters, the parent, and the caller's scope."""
        given = context.filters
        query = given.query() if isinstance(given, FilterSet) else as_query(given)
        return query.where(
            *await reach.criteria(self.router, context.request, context.ids)
        )

    async def export(self, context: RouteContext, query: Query, media: str) -> Response:
        """Every match of ``query``, streamed as CSV or NDJSON.

        Raises:
            ExportTooLarge: More than ``max_export`` match.
        """
        router = self.router
        async with context.uow as uow:
            total = await router.repository(uow).count(
                query.criteria, include_deleted=query.include_deleted
            )
        if total > router.max_export:
            raise ExportTooLarge
        hidden = await presenting.hidden_fields(router, context.request)
        columns = [
            name
            for name in router.schema.model_fields
            if name not in hidden
            and name != "links"
            and (router.list_fields is None or name in router.list_fields)
        ]

        async def body() -> AsyncIterator[str]:
            if media == CSV:
                yield csv_rows([columns])
            after: Sequence[object] | None = None
            sent = 0
            while sent < router.max_export:
                async with context.uow as uow:
                    page = await router.repository(uow).paginate(
                        query,
                        limit=min(EXPORT_CHUNK, router.max_export - sent),
                        after=after,
                    )
                documents = [
                    presenting.dump(router, context, item, hidden, router.list_fields)
                    for item in page.items
                ]
                sent += len(documents)
                yield (
                    csv_rows(
                        [[each.get(name) for name in columns] for each in documents]
                    )
                    if media == CSV
                    else ndjson_lines(documents)
                )
                if not page.has_more:
                    break
                after = page.next_position

        headers = {"Vary": "Accept"}
        if media == CSV:
            headers["Content-Disposition"] = (
                f'attachment; filename="{router.plural}.csv"'
            )
        return StreamingResponse(body(), media_type=media, headers=headers)
