"""API versions by URL prefix, and deprecation headers (RFC 9745, RFC 8594).

A version is a router mounted under its own prefix; its routes' operation
ids are the version and the route's name (``v1_list_products``), so ``/v1``
and ``/v2`` can share route names, and ids stay stable::

    v1 = versioned_router("v1", deprecation=V1_SUNSET)
    v1.include_router(products_v1)
    v2 = versioned_router("v2")
    v2.include_router(products_v2)
    router.include_router(v1)
    router.include_router(v2)

A ``Deprecation`` marks the routes it guards, a whole router or one route::

    V1_SUNSET = Deprecation(
        since=datetime(2026, 11, 1, tzinfo=UTC),
        sunset=datetime(2027, 5, 1, tzinfo=UTC),
        link="https://api.example.com/docs/migrate-to-v2",
    )

    @router.get("/old", dependencies=[Depends(V1_SUNSET)])

Every response those routes answer, errors included, then carries
``Deprecation: @<seconds>``, ``Sunset: <HTTP date>``, and ``Link: <...>;
rel="deprecation"``, and OpenAPI marks their operations deprecated, with the
headers and the sunset date (``x-sunset``). The most specific deprecation
wins: a route's own over its router's.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from email.utils import format_datetime
from typing import Any, Final
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from fastapi.routing import APIRoute
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.exceptions import ImproperlyConfigured

SCOPE_KEY: Final = "domainkit.deprecation"
"""Where the deprecation of the route serving a request is kept in its scope."""
_VERSION: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_UNSAFE_LINK: Final = re.compile(r"[\x00-\x20<>\"\x7f]")
"""Characters a URL in a ``Link`` header may not hold (no header injection)."""


def _moment(value: datetime | date, name: str) -> datetime:
    """``value`` as an aware datetime; a date is its midnight, UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ImproperlyConfigured(f"Deprecation {name} needs a time zone.")
        return value.astimezone(UTC)
    return datetime.combine(value, time(), tzinfo=UTC)


@dataclass(frozen=True, slots=True, init=False)
class Deprecation:
    """When routes were deprecated, when they go, and where to read why.

    A FastAPI dependency: put it on a router, an ``include_router`` call,
    or a route, as ``Depends(deprecation)``.
    """

    since: datetime
    """When the routes were, or will be, deprecated (``Deprecation``)."""
    sunset: datetime | None
    """When they stop answering (``Sunset``); after ``since``."""
    link: str | None
    """An absolute http(s) URL describing the deprecation (``Link``)."""

    def __init__(
        self,
        *,
        since: datetime | date,
        sunset: datetime | date | None = None,
        link: str | None = None,
    ) -> None:
        """Check the dates and the link once, when routes are declared."""
        start = _moment(since, "since")
        end = None if sunset is None else _moment(sunset, "sunset")
        if end is not None and end <= start:
            raise ImproperlyConfigured("A deprecation's sunset must follow its since.")
        if link is not None:
            parts = urlsplit(link)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.netloc
                or _UNSAFE_LINK.search(link)
            ):
                raise ImproperlyConfigured(
                    "A deprecation's link is an absolute http(s) URL without "
                    "spaces, quotes, or angle brackets."
                )
        object.__setattr__(self, "since", start)
        object.__setattr__(self, "sunset", end)
        object.__setattr__(self, "link", link)

    async def __call__(self, connection: HTTPConnection) -> None:
        """Mark the request: its response gets this deprecation's headers."""
        connection.scope[SCOPE_KEY] = self

    def headers(self) -> list[tuple[str, str]]:
        """``Deprecation``, and ``Sunset`` and ``Link`` when they are known."""
        headers = [("Deprecation", f"@{int(self.since.timestamp())}")]
        if self.sunset is not None:
            headers.append(("Sunset", format_datetime(self.sunset, usegmt=True)))
        if self.link is not None:
            headers.append(("Link", f'<{self.link}>; rel="deprecation"'))
        return headers

    def openapi(self) -> dict[str, Any]:
        """What OpenAPI adds to each operation: the dates and the link."""
        extra: dict[str, Any] = {"x-deprecated-since": self.since.isoformat()}
        if self.sunset is not None:
            extra["x-sunset"] = self.sunset.isoformat()
        if self.link is not None:
            extra["externalDocs"] = {"url": self.link, "description": "Deprecation"}
        return extra


class DeprecationHeadersMiddleware:
    """Add the headers of the deprecation the route marked in the scope.

    ``get_asgi_application`` installs it innermost, so it sees every
    response a route answers, also those the error handlers build.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add the headers to the response start of a deprecated route."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            deprecation = scope.get(SCOPE_KEY)
            if message["type"] == "http.response.start" and isinstance(
                deprecation, Deprecation
            ):
                headers = MutableHeaders(scope=message)
                for name, value in deprecation.headers():
                    if name == "Link":
                        headers.append(name, value)
                    elif name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class VersionedIds:
    """Operation ids of a version's routes: ``<version>_<route name>``.

    They do not depend on ``OPENAPI_OPERATION_IDS``: FastAPI asks for them
    as routers are included, which may be before settings are loaded.
    """

    def __init__(self, version: str) -> None:
        """Prefix route names with ``version``."""
        self.version = version

    def __call__(self, route: APIRoute) -> str:
        """``<version>_<route name>``, such as ``v1_list_products``."""
        return f"{self.version}_{route.name}"


def versioned_router(
    version: str,
    *,
    deprecation: Deprecation | None = None,
    dependencies: Sequence[Any] = (),
    **options: Any,  # noqa: ANN401 - APIRouter's own options.
) -> APIRouter:
    """A router for one API version, served under ``/<version>``.

    Its routes' operation ids start with the version. With ``deprecation``,
    every route of the version is deprecated. ``options`` go to
    ``APIRouter`` (``tags``, ``responses``, ...).
    """
    if not _VERSION.fullmatch(version):
        raise ImproperlyConfigured(
            f"API version {version!r} must be letters, digits, '.', '_', or '-' "
            "(at most 32), such as 'v1'."
        )
    guards = [*dependencies]
    if deprecation is not None:
        guards.insert(0, Depends(deprecation))
    return APIRouter(
        prefix=f"/{version}",
        dependencies=guards,
        deprecated=True if deprecation is not None else None,
        generate_unique_id_function=VersionedIds(version),
        **options,
    )
