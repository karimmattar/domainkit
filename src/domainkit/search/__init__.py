"""Search engines: documents indexed beside the database, found by words.

For words in one table, a repository's ``F("body").search("late fee")``
is enough (PostgreSQL's full-text search). For relevance across fields,
typo tolerance, and facets, keep an index in a search engine::

    SEARCH = {
        "BACKEND": "domainkit.search.meilisearch.MeilisearchBackend",
        "OPTIONS": {"url": "https://search.internal:7700", "api_key": env.str(...)},
    }

    search = search_backend()
    await search.ensure("products", searchable=["name", "description"],
                        filterable=["shop_id"])
    await search.index("products", [{"id": str(p.id), "name": p.name, ...}])
    found = await search.search("products", "red shoes", filters={"shop_id": shop})

``found.ids`` are the matching documents' ids, best first: read the
aggregates from their repository, which keeps the rules. Index from an
event handler of the aggregate's events, so the index follows commits.
The backends call through ``domainkit.http.client`` (the ``httpclient``
extra), with no SDK; ``MemorySearchBackend`` is for tests.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final

from domainkit.exceptions import DomainkitError

__all__ = [
    "MemorySearchBackend",
    "SearchBackend",
    "SearchError",
    "SearchResult",
    "search_backend",
]

INDEX_NAME: Final = r"^[a-z0-9][a-z0-9_-]{0,63}$"
"""What an index name may be, in every engine."""
MAX_LIMIT: Final = 1000


class SearchError(DomainkitError):
    """The search engine refused, or could not be reached; never names a document."""


@dataclass(frozen=True, slots=True)
class SearchResult:
    """The documents a search found: their ids, best first, and about how many match."""

    ids: list[str]
    total: int


class SearchBackend(ABC):
    """An index of documents per name, each with an ``id``, found by words."""

    deployable: ClassVar[bool] = True
    """False for backends made for tests; deployments refuse them."""

    @abstractmethod
    async def ensure(
        self,
        index: str,
        *,
        searchable: Sequence[str] = (),
        filterable: Sequence[str] = (),
    ) -> None:
        """Make the index if missing; set what it searches and filters by."""

    @abstractmethod
    async def index(self, index: str, documents: Sequence[Mapping[str, Any]]) -> None:
        """Add or replace ``documents``, each with an ``id``."""

    @abstractmethod
    async def delete(self, index: str, ids: Sequence[str]) -> None:
        """Remove the documents of ``ids``; missing ones are no error."""

    @abstractmethod
    async def search(
        self,
        index: str,
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResult:
        """The documents matching ``query`` whose fields equal ``filters``."""


def check_page(index: str, limit: int, offset: int) -> None:
    """Refuse a bad index name or page.

    Raises:
        ValueError: The name is not one, or the page is out of range.
    """
    import re

    if not re.fullmatch(INDEX_NAME, index):
        raise ValueError("an index is named with a-z, 0-9, '_', '-'")
    if not (1 <= limit <= MAX_LIMIT and offset >= 0):
        raise ValueError(f"a search takes 1 to {MAX_LIMIT} results, from 0 on")


class MemorySearchBackend(SearchBackend):
    """Indexes in this process, for tests: every word of the query in a document."""

    deployable = False

    def __init__(self) -> None:
        """No indexes."""
        self.indexes: dict[str, dict[str, dict[str, Any]]] = {}
        self.searchable: dict[str, tuple[str, ...]] = {}

    async def ensure(
        self,
        index: str,
        *,
        searchable: Sequence[str] = (),
        filterable: Sequence[str] = (),
    ) -> None:
        """Make the index."""
        check_page(index, 1, 0)
        self.indexes.setdefault(index, {})
        self.searchable[index] = tuple(searchable)

    async def index(self, index: str, documents: Sequence[Mapping[str, Any]]) -> None:
        """Keep the documents by id."""
        held = self.indexes.setdefault(index, {})
        for document in documents:
            held[str(document["id"])] = dict(document)

    async def delete(self, index: str, ids: Sequence[str]) -> None:
        """Forget them."""
        for each in ids:
            self.indexes.get(index, {}).pop(str(each), None)

    async def search(
        self,
        index: str,
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResult:
        """Documents holding every word, in the order they were indexed."""
        check_page(index, limit, offset)
        words = query.lower().split()
        fields = self.searchable.get(index) or ()
        found = [
            key
            for key, document in self.indexes.get(index, {}).items()
            if all(document.get(k) == v for k, v in (filters or {}).items())
            and all(
                any(
                    word in str(document.get(f, "")).lower()
                    for f in (fields or document)
                )
                for word in words
            )
        ]
        return SearchResult(found[offset : offset + limit], len(found))


def search_backend() -> SearchBackend:
    """A new instance of ``SEARCH.BACKEND``, with ``SEARCH.OPTIONS``.

    Raises:
        ImproperlyConfigured: ``SEARCH`` is not set, or names no backend.
    """
    from domainkit.conf import settings
    from domainkit.exceptions import ImproperlyConfigured
    from domainkit.utils.module_loading import import_backend

    config = settings.SEARCH
    if config is None:
        raise ImproperlyConfigured("Set SEARCH to use a search engine.")
    try:
        backend = import_backend(config.BACKEND)
    except ImportError as exc:
        raise ImproperlyConfigured(f"SEARCH.BACKEND: {exc}") from exc
    if not (isinstance(backend, type) and issubclass(backend, SearchBackend)):
        raise ImproperlyConfigured(
            f"SEARCH.BACKEND: {config.BACKEND!r} is not a SearchBackend."
        )
    instance: SearchBackend = backend(**config.OPTIONS)
    return instance
