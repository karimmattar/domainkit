"""OpenSearch (or Elasticsearch) as a ``SearchBackend``.

::

    SEARCH = {
        "BACKEND": "domainkit.search.opensearch.OpenSearchBackend",
        "OPTIONS": {"url": "https://search.internal:9200",
                    "username": "app", "password": env.str(...)},
    }

A document's ``id`` is its ``_id``. Writes go through ``_bulk`` and are
refreshed before the call returns (``refresh=wait_for``), so a search
right after sees them. A query matches every word across the
``searchable`` fields (``multi_match``, ``operator: and``); filters are
``term`` equalities.
"""

import base64
import json
from collections.abc import Mapping, Sequence
from typing import Any, Final
from urllib.parse import quote

from . import SearchBackend, SearchError, SearchResult, check_page
from ._http import call

__all__ = ["OpenSearchBackend"]

ENGINE: Final = "OpenSearch"


class OpenSearchBackend(SearchBackend):
    """Indexes in an OpenSearch cluster (see the module)."""

    def __init__(
        self,
        url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        """Call ``url``, as ``username`` (HTTP Basic) if given."""
        from domainkit.http.client import HttpClient

        self.url = url.rstrip("/")
        self._headers: dict[str, str] = {}
        if username is not None:
            secret = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
            self._headers["Authorization"] = f"Basic {secret}"
        self.client = HttpClient(base_url=self.url, timeout=timeout, propagate=False)
        self.fields: dict[str, list[str]] = {}

    def __repr__(self) -> str:
        """The cluster; never the password."""
        return f"OpenSearchBackend(url={self.url!r})"

    async def ensure(
        self,
        index: str,
        *,
        searchable: Sequence[str] = (),
        filterable: Sequence[str] = (),
    ) -> None:
        """Make the index (one that exists is fine): text and keyword fields."""
        check_page(index, 1, 0)
        properties = {name: {"type": "text"} for name in searchable}
        properties |= {name: {"type": "keyword"} for name in filterable}
        answer = await call(
            self.client,
            ENGINE,
            "PUT",
            f"/{quote(index)}",
            json={"mappings": {"properties": properties}},
            headers=self._headers,
            accept=(200, 400),
        )
        error = (answer or {}).get("error") if isinstance(answer, dict) else None
        if error and "already_exists" not in str(
            error.get("type", "") if isinstance(error, dict) else error
        ):
            raise SearchError("OpenSearch refused the index.")
        self.fields[index] = list(searchable)

    async def index(self, index: str, documents: Sequence[Mapping[str, Any]]) -> None:
        """``_bulk`` index actions, refreshed before returning."""
        check_page(index, 1, 0)
        lines = []
        for document in documents:
            lines.append(
                json.dumps({"index": {"_index": index, "_id": str(document["id"])}})
            )
            lines.append(json.dumps(dict(document), default=str))
        await self._bulk(lines)

    async def delete(self, index: str, ids: Sequence[str]) -> None:
        """``_bulk`` delete actions; missing documents are no error."""
        check_page(index, 1, 0)
        await self._bulk(
            [
                json.dumps({"delete": {"_index": index, "_id": str(each)}})
                for each in ids
            ]
        )

    async def _bulk(self, lines: list[str]) -> None:
        """Send ``lines`` to ``_bulk``; refuse an answer with errors.

        A ``delete`` of a missing document (404) is not one.
        """
        if not lines:
            return
        answer = await call(
            self.client,
            ENGINE,
            "POST",
            "/_bulk?refresh=wait_for",
            content=("\n".join(lines) + "\n").encode(),
            headers={**self._headers, "Content-Type": "application/x-ndjson"},
        )
        if isinstance(answer, dict) and answer.get("errors"):
            failed = [
                result
                for item in answer.get("items", [])
                for result in item.values()
                if result.get("status", 200) >= 300 and result.get("status") != 404
            ]
            if failed:
                raise SearchError(f"OpenSearch refused {len(failed)} of the actions.")

    async def search(
        self,
        index: str,
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResult:
        """``_search``: every word across the searchable fields, then the filters."""
        check_page(index, limit, offset)
        match: dict[str, Any] = {"query": query, "operator": "and"}
        if fields := self.fields.get(index):
            match["fields"] = fields
        body = {
            "from": offset,
            "size": limit,
            "_source": False,
            "track_total_hits": True,
            "query": {
                "bool": {
                    "must": [{"multi_match": match}],
                    "filter": [{"term": {k: v}} for k, v in (filters or {}).items()],
                }
            },
        }
        answer = await call(
            self.client,
            ENGINE,
            "POST",
            f"/{quote(index)}/_search",
            json=body,
            headers=self._headers,
        )
        try:
            hits = answer["hits"]
            ids = [str(hit["_id"]) for hit in hits["hits"]]
            total = hits["total"]
            count = int(total["value"] if isinstance(total, dict) else total)
        except (KeyError, TypeError, ValueError):
            raise SearchError("OpenSearch answered without hits.") from None
        return SearchResult(ids, count)
