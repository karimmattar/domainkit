"""Meilisearch as a ``SearchBackend``.

::

    SEARCH = {
        "BACKEND": "domainkit.search.meilisearch.MeilisearchBackend",
        "OPTIONS": {"url": "https://search.internal:7700", "api_key": env.str(...)},
    }

An index's primary key is ``id``. Meilisearch takes writes as tasks it
runs in order: ``index`` and ``delete`` return once it has queued them,
so a search right after may not see them yet. Filters are equalities,
written as Meilisearch's filter expression with every value quoted.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final
from urllib.parse import quote

from . import SearchBackend, SearchError, SearchResult, check_page
from ._http import call

__all__ = ["MeilisearchBackend"]

ENGINE: Final = "Meilisearch"


class MeilisearchBackend(SearchBackend):
    """Indexes in a Meilisearch server (see the module)."""

    def __init__(
        self, url: str, api_key: str | None = None, *, timeout: float = 10.0
    ) -> None:
        """Call ``url``, with ``api_key`` (a key allowed what the calls do)."""
        from domainkit.http.client import HttpClient

        self.url = url.rstrip("/")
        self._headers = (
            {} if api_key is None else {"Authorization": f"Bearer {api_key}"}
        )
        self.client = HttpClient(base_url=self.url, timeout=timeout, propagate=False)

    def __repr__(self) -> str:
        """The server; never the key."""
        return f"MeilisearchBackend(url={self.url!r})"

    async def ensure(
        self,
        index: str,
        *,
        searchable: Sequence[str] = (),
        filterable: Sequence[str] = (),
    ) -> None:
        """Make the index (a 409, it exists, is fine), then set its attributes."""
        check_page(index, 1, 0)
        await call(
            self.client,
            ENGINE,
            "POST",
            "/indexes",
            json={"uid": index, "primaryKey": "id"},
            headers=self._headers,
            accept=(200, 201, 202, 409),
        )
        settings: dict[str, Any] = {}
        if searchable:
            settings["searchableAttributes"] = list(searchable)
        if filterable:
            settings["filterableAttributes"] = list(filterable)
        if settings:
            await call(
                self.client,
                ENGINE,
                "PATCH",
                f"/indexes/{quote(index)}/settings",
                json=settings,
                headers=self._headers,
            )

    async def index(self, index: str, documents: Sequence[Mapping[str, Any]]) -> None:
        """Add or replace the documents (a task)."""
        check_page(index, 1, 0)
        if documents:
            await call(
                self.client,
                ENGINE,
                "POST",
                f"/indexes/{quote(index)}/documents",
                json=[dict(document) for document in documents],
                headers=self._headers,
            )

    async def delete(self, index: str, ids: Sequence[str]) -> None:
        """Delete the documents of ``ids`` (a task)."""
        check_page(index, 1, 0)
        if ids:
            await call(
                self.client,
                ENGINE,
                "POST",
                f"/indexes/{quote(index)}/documents/delete-batch",
                json=[str(each) for each in ids],
                headers=self._headers,
            )

    async def search(
        self,
        index: str,
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResult:
        """``/search``: the hits' ids, and the estimated total."""
        check_page(index, limit, offset)
        body: dict[str, Any] = {
            "q": query,
            "limit": limit,
            "offset": offset,
            "attributesToRetrieve": ["id"],
        }
        if filters:
            body["filter"] = " AND ".join(
                f"{_attribute(name)} = {json.dumps(_text(value))}"
                for name, value in filters.items()
            )
        answer = await call(
            self.client,
            ENGINE,
            "POST",
            f"/indexes/{quote(index)}/search",
            json=body,
            headers=self._headers,
        )
        try:
            hits = [str(hit["id"]) for hit in answer["hits"]]
            total = int(
                answer.get("estimatedTotalHits", answer.get("totalHits", len(hits)))
            )
        except (KeyError, TypeError, ValueError):
            raise SearchError("Meilisearch answered without hits.") from None
        return SearchResult(hits, total)


def _attribute(name: str) -> str:
    """A filterable attribute's name, refused if it could write an expression.

    Raises:
        ValueError: It is not letters, digits, ``_``, ``.``.
    """
    if not name.replace("_", "").replace(".", "").isalnum():
        raise ValueError(f"{name!r} is not an attribute name")
    return name


def _text(value: Any) -> str:  # noqa: ANN401 - any filter value.
    """A filter value as text: Meilisearch compares quoted values."""
    return value if isinstance(value, str) else json.dumps(value)
