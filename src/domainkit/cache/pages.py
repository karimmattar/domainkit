"""``@cache_page``: cache what a GET endpoint returns (Django's per-view cache).

The endpoint's return value is cached, typed by its return annotation as
with ``@cached``, and FastAPI serializes it on every request, hit or miss,
so ``response_model`` filtering always applies::

    @router.get("/products", dependencies=[Public])
    @cache_page(ttl=60)
    async def list_products(category: str | None = None) -> list[ProductOut]: ...

Entries are keyed by the endpoint, the path, the query, the request's
language, and the headers listed in ``vary``. Route dependencies (rules,
throttles) run first, on every request, so a cached answer never reaches a
caller who may not see it.

A response depends on who asks unless proven otherwise, so requests that
carry credentials are never served from, or stored in, the cache, unless
the page varies on the user: ``vary_on_user=True`` keys entries by actor
and marks them ``private``. Requests with credentials no middleware
accepted (no actor) are never cached.

Cached pages override the framework's ``Cache-Control: no-store`` on
purpose: clients may keep them, and must revalidate them (``no-cache``)
unless ``max_age`` says otherwise. Each answer has an ``ETag``, and a
request whose ``If-None-Match`` names it gets 304 without a body.
"""

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from functools import update_wrapper
from typing import Any, Final, get_type_hints

from fastapi import Request, Response, status
from pydantic_core import to_json

from domainkit.cache.core import DEFAULT_TTL, TTL, adapter
from domainkit.cache.handler import caches
from domainkit.conf import DEFAULT_CACHE_ALIAS
from domainkit.context import current_request
from domainkit.i18n.translation import get_language
from domainkit.utils.headers import patch_vary

REQUEST: Final = "_cache_page_request"
RESPONSE: Final = "_cache_page_response"
"""Parameters added to the endpoint: FastAPI passes the request and response."""
CACHEABLE_METHODS: Final = frozenset({"GET", "HEAD"})

type Endpoint = Callable[..., Awaitable[Any]]


def cache_page(
    ttl: TTL = DEFAULT_TTL,
    *,
    cache: str = DEFAULT_CACHE_ALIAS,
    vary: Sequence[str] = (),
    vary_on_user: bool = False,
    max_age: int = 0,
    name: str | None = None,
) -> Callable[[Endpoint], Endpoint]:
    """Cache what an async GET endpoint returns, for ``ttl``, in ``cache``.

    ``vary`` lists request headers the answer depends on (``X-Tenant``).
    ``vary_on_user`` caches answers to users too, one entry per actor.
    ``max_age`` lets clients reuse an answer that long without asking
    again. ``name`` (default: the endpoint's import path) starts every key.

    Raises:
        TypeError: The endpoint is not async, or returns a ``Response``.
        ValueError: ``max_age`` is negative.
    """
    if max_age < 0:
        raise ValueError("max_age cannot be negative.")
    headers = tuple(header.lower() for header in vary)

    def decorate(endpoint: Endpoint) -> Endpoint:
        if not inspect.iscoroutinefunction(endpoint):
            raise TypeError("@cache_page decorates async endpoints.")
        hints = get_type_hints(endpoint, include_extras=True)
        returns = hints.get("return", Any)
        if inspect.isclass(returns) and issubclass(returns, Response):
            raise TypeError(
                "@cache_page caches values FastAPI serializes, not Response objects."
            )
        prefix = name or f"{endpoint.__module__}.{endpoint.__qualname__}"
        signature = _signature(endpoint, hints)
        request_name = _parameter_of(signature, Request)
        response_name = _parameter_of(signature, Response)

        async def page(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - the endpoint's.
            request: Request = kwargs[request_name]
            response: Response = kwargs[response_name]
            for added in (REQUEST, RESPONSE):  # Ours, not the endpoint's.
                kwargs.pop(added, None)
            user = _user(request)
            if (
                request.method not in CACHEABLE_METHODS
                or user == _UNCHECKED
                or (user is not None and not vary_on_user)
            ):
                return await endpoint(*args, **kwargs)
            key = _key(prefix, request, headers, user)

            async def compute() -> Any:  # noqa: ANN401 - the endpoint's.
                # Typed now, so fields the return type leaves out are not cached.
                result = await endpoint(*args, **kwargs)
                return adapter(returns).validate_python(result, from_attributes=True)

            value = await caches[cache].get_or_set(key, compute, returns, ttl)
            etag = f'W/"{hashlib.sha256(to_json(value)).hexdigest()[:32]}"'
            control = "private" if vary_on_user else "public"
            answer = {
                "ETag": etag,
                "Cache-Control": f"{control}, "
                + (f"max-age={max_age}" if max_age else "no-cache"),
            }
            if _matches(request.headers.get("if-none-match"), etag):
                unchanged = Response(status_code=status.HTTP_304_NOT_MODIFIED)
                unchanged.headers.update(answer)
                patch_vary(unchanged.headers, ["Authorization", *vary])
                return unchanged
            response.headers.update(answer)
            patch_vary(response.headers, ["Authorization", *vary])
            return value

        update_wrapper(page, endpoint)
        page.__signature__ = signature  # type: ignore[attr-defined]
        return page

    return decorate


_UNCHECKED: Final = ""
"""Credentials no middleware accepted: whose they are is unknown."""


def _user(request: Request) -> str | None:
    """The actor, ``""`` for credentials without one, or None for anonymous callers."""
    context = current_request()
    if context is not None and context.actor:
        return context.actor
    return _UNCHECKED if "authorization" in request.headers else None


def _key(
    prefix: str, request: Request, headers: Sequence[str], user: str | None
) -> str:
    """The entry for this request: path, query, language, headers, and user."""
    query = sorted(request.query_params.multi_items(), key=lambda item: item[0])
    parts = [
        request.url.path,
        query,
        get_language(),
        [request.headers.get(header) for header in headers],
        user,
    ]
    canonical = json.dumps(parts, separators=(",", ":"))
    return f"page:{prefix}:{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"


def _matches(if_none_match: str | None, etag: str) -> bool:
    """Whether ``If-None-Match`` names ``etag`` (or ``*``), compared weakly."""
    if not if_none_match:
        return False
    wanted = etag.removeprefix("W/")
    for tag in if_none_match.split(","):
        tag = tag.strip()
        if tag == "*" or tag.removeprefix("W/") == wanted:
            return True
    return False


def _signature(endpoint: Endpoint, hints: dict[str, Any]) -> inspect.Signature:
    """The endpoint's signature, annotations resolved, with the request and response.

    FastAPI reads parameters from it. It would evaluate annotations written
    as strings (``from __future__ import annotations``) in this module,
    where the endpoint's names are unknown, so they are resolved here.
    FastAPI passes one ``Request`` and one ``Response`` parameter, so the
    endpoint's own are used when it has them.
    """
    signature = inspect.signature(endpoint)
    parameters = [
        parameter.replace(annotation=hints.get(name, parameter.annotation))
        for name, parameter in signature.parameters.items()
    ]
    if {REQUEST, RESPONSE} & set(signature.parameters):
        raise TypeError(f"@cache_page needs the parameter names {REQUEST}, {RESPONSE}.")
    keyword = inspect.Parameter.KEYWORD_ONLY
    added = [
        inspect.Parameter(name, keyword, annotation=kind)
        for name, kind in ((REQUEST, Request), (RESPONSE, Response))
        if not any(_is(parameter.annotation, kind) for parameter in parameters)
    ]
    variadic = [p for p in parameters if p.kind is inspect.Parameter.VAR_KEYWORD]
    fixed = [p for p in parameters if p.kind is not inspect.Parameter.VAR_KEYWORD]
    return signature.replace(
        parameters=[*fixed, *added, *variadic],
        return_annotation=hints.get("return", signature.return_annotation),
    )


def _parameter_of(signature: inspect.Signature, kind: type) -> str:
    """The name of the parameter FastAPI passes a ``kind`` (request or response)."""
    return next(
        name
        for name, parameter in signature.parameters.items()
        if _is(parameter.annotation, kind)
    )


def _is(annotation: object, kind: type) -> bool:
    """Whether a parameter annotated ``annotation`` receives a ``kind``."""
    return inspect.isclass(annotation) and issubclass(annotation, kind)
