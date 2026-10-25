"""``@cache_page``: cache what a GET endpoint returns (Django's per-view cache).

The endpoint's return value is cached, typed by its return annotation as
with ``@cached``, and FastAPI serializes it on every request, hit or miss,
so ``response_model`` filtering always applies::

    @router.get("/products", dependencies=[Public])
    @cache_page(ttl=60)
    async def list_products(category: str | None = None) -> list[ProductOut]: ...

Entries are keyed by the endpoint, the URL (scheme, host, and path, as
Django's absolute URIs), the query, the request's language, and the
headers listed in ``vary``. Route dependencies (rules,
throttles) run first, on every request, so a cached answer never reaches a
caller who may not see it.

A response depends on who asks unless proven otherwise, so requests that
carry credentials are never served from, or stored in, the cache, unless
the page varies on the user: ``vary_on_user=True`` keys entries by actor
and marks them ``private``. Credentials are the actor, the ``Credentials``
an authentication middleware accepted (``request.auth``: a token, a
cookie, a client certificate), or an ``Authorization`` header. Requests
with credentials but no actor or user id are never cached.

Cached pages override the framework's ``Cache-Control: no-store`` on
purpose: clients may keep them, and must revalidate them (``no-cache``)
unless ``max_age`` says otherwise. Each answer has an ``ETag``, and a
request whose ``If-None-Match`` names it gets 304 without a body.
"""

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from contextvars import copy_context
from functools import partial, update_wrapper
from typing import Annotated, Any, Final, get_args, get_origin, get_type_hints

from fastapi import Request, Response, params, status
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from pydantic_core import to_json

from domainkit import deadlines
from domainkit.cache.core import DEFAULT_TTL, TTL, adapter
from domainkit.cache.handler import caches
from domainkit.conf import DEFAULT_CACHE_ALIAS
from domainkit.context import current_request
from domainkit.i18n.translation import get_language
from domainkit.utils.headers import none_match, patch_vary

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
    borrows: bool = True,
) -> Callable[[Endpoint], Endpoint]:
    """Cache what an async GET endpoint returns, for ``ttl``, in ``cache``.

    ``vary`` lists request headers the answer depends on (``X-Tenant``).
    ``vary_on_user`` caches answers to users too, one entry per actor.
    ``max_age`` lets clients reuse an answer that long without asking
    again. ``name`` (default: the endpoint's import path) starts every key.

    The endpoint runs for every caller of its entry, as the caller that
    missed it. By default it may use what that request holds (its
    dependencies, ``request.state``, a context variable a middleware set):
    the computation borrows the request (``get_or_set(..., borrows=True)``),
    runs under its deadline, and ends with it. ``borrows=False`` says the
    endpoint uses nothing the request holds: its computation then goes on
    after the request, fills the cache, and runs under the cache's
    ``COMPUTE_TIMEOUT``. Dependencies that hold a resource (written with
    ``yield``) are refused then, the endpoint's own here and the route's,
    router's, and application's when a request comes, which then borrows.

    Raises:
        TypeError: The endpoint is not async, returns a ``Response``, or
            takes a dependency that holds a resource with ``borrows=False``.
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
        if not borrows and any(_holds(call) for call in _dependencies(signature)):
            raise TypeError(
                "@cache_page(borrows=False) cannot run an endpoint whose "
                "dependencies hold a resource (a dependency with yield): its "
                "computation would use it after the request closed it."
            )
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
            caller = copy_context()

            async def compute() -> Any:  # noqa: ANN401 - the endpoint's.
                # Computations run in a clean context; the endpoint runs as
                # the caller, whose user and language the key holds, so every
                # caller sharing the entry shares them, under the
                # computation's deadline, not the caller's.
                caller.run(deadlines._current.set, deadlines.current_deadline())
                result = await asyncio.get_running_loop().create_task(
                    endpoint(*args, **kwargs), context=caller
                )
                # Typed now, so fields the return type leaves out are not cached.
                return adapter(returns).validate_python(result, from_attributes=True)

            value = await caches[cache].get_or_set(
                key, compute, returns, ttl, borrows=borrows or _route_holds(request)
            )
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
    """The actor, ``""`` for credentials without one, or None for anonymous callers.

    Without an actor (no ``RequestContextMiddleware``), credentials that
    middleware accepted (``request.auth``) still count, by their user's id:
    a cookie or a client certificate sends no ``Authorization`` header.
    """
    context = current_request()
    if context is not None and context.actor:
        return context.actor
    credentials = request.scope.get("auth")
    if credentials is not None:
        user_id = getattr(getattr(credentials, "user", None), "id", None)
        return _UNCHECKED if user_id is None else str(user_id)
    return _UNCHECKED if "authorization" in request.headers else None


def _key(
    prefix: str, request: Request, headers: Sequence[str], user: str | None
) -> str:
    """The entry for this request: URL, query, language, headers, and user.

    The URL has the scheme and the host, so tenants chosen by ``Host``, and
    answers that link to themselves, never share an entry.
    """
    query = sorted(request.query_params.multi_items(), key=lambda item: item[0])
    url = request.url
    parts = [
        f"{url.scheme}://{url.netloc}{url.path}",
        query,
        get_language(),
        [request.headers.get(header) for header in headers],
        user,
    ]
    canonical = json.dumps(parts, separators=(",", ":"))
    return f"page:{prefix}:{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"


def _matches(if_none_match: str | None, etag: str) -> bool:
    """Whether ``If-None-Match`` names ``etag`` (or ``*``), compared weakly.

    Resources compare their tags the same way (``utils.headers.none_match``).
    """
    return none_match(if_none_match, etag)


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


def _dependencies(signature: inspect.Signature) -> list[Callable[..., Any]]:
    """What FastAPI calls to resolve the parameters of ``signature``.

    ``Depends()`` without a callable means the parameter's annotation.
    """
    found: list[Callable[..., Any]] = []
    for parameter in signature.parameters.values():
        annotation, markers = parameter.annotation, [parameter.default]
        if get_origin(annotation) is Annotated:
            annotation, *metadata = get_args(annotation)
            markers += metadata
        found += [
            marker.dependency or annotation
            for marker in markers
            if isinstance(marker, params.Depends)
        ]
    return found


def _forms(call: Any) -> list[Any]:  # noqa: ANN401 - any dependency.
    """``call`` as FastAPI looks at it: through ``partial`` and through ``wraps``."""
    while isinstance(call, partial):
        call = call.func
    return [call, inspect.unwrap(call)]


def _yields(call: Any) -> bool:  # noqa: ANN401 - any dependency.
    """Whether FastAPI takes a dependency for one that holds a resource.

    As FastAPI 0.143 decides it (``fastapi.dependencies.models``): the
    function, or its ``__call__``, yields, seen through ``functools.partial``
    (nested too) and ``functools.wraps``; a class is built, so it never does.
    FastAPI closes such a dependency when the request ends.
    """
    if any(_generator(form) for form in _forms(call)):
        return True
    if any(inspect.isclass(form) for form in _forms(call)):
        return False
    return any(
        _generator(inner) for form in _forms(call) for inner in _forms(form.__call__)
    )


def _generator(call: object) -> bool:
    """Whether calling ``call`` makes a generator, async or not."""
    return inspect.isgeneratorfunction(call) or inspect.isasyncgenfunction(call)


def _holds(call: Callable[..., Any]) -> bool:
    """Whether a dependency, or one beneath it, holds a resource.

    The dependencies beneath are found as FastAPI finds them (``get_dependant``).
    """
    return _tree_holds(get_dependant(path="", call=call))


def _tree_holds(dependant: Dependant) -> bool:
    """Whether ``dependant``, or a dependency beneath it, holds a resource."""
    stack = [dependant]
    while stack:
        dependency = stack.pop()
        if _yields(dependency.call):
            return True
        stack += dependency.dependencies
    return False


def _route_holds(request: Request) -> bool:
    """Whether the request's route or application adds a dependency with ``yield``.

    The route's own dependencies and its router's are in ``route.dependant``;
    the application's in its router's. Those of routers it was included
    through on the way are not in the request, so they cannot be seen here.
    """
    dependant = getattr(request.scope.get("route"), "dependant", None)
    if dependant is not None and _tree_holds(dependant):
        return True
    application = getattr(request.scope.get("router"), "dependencies", ())
    return any(_holds(marker.dependency) for marker in application)


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
