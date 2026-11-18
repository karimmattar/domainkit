"""A route that takes a file, stores it, and runs a command with it.

``ResourceRouter.upload_route`` is a command route (``commands``) whose
command receives a stored upload (``domainkit.storage.StoredUpload``) in
one of its fields, and optionally the file's SHA-256 in another. The
request's body is the file itself, its name in ``X-Filename``
(percent-encoded UTF-8), as a phone that signs the body it sends
(``DeviceSigned``) can send it; or a ``multipart/form-data`` form whose
part named after the field holds it. The body limit is the file's, with
room for a form.

The route checks what a command route checks, and before it reads a
byte of the body: its rules, then on one aggregate, the aggregate within
the caller's reach (404), the rules on it (403), and ``If-Match`` (412,
428). It then receives the file as ``save_upload`` does (its type from
its content, its type's check, the size bound), and stores it before the
service runs, outside the service's transaction. The reach and
``If-Match`` are checked again in it, as a command route checks them. A
request whose unit of work did not commit deletes the file.

A route whose rules ask for an authorization of the act gets the twin a
command route gets (``authorizing``): it checks the reach first too,
takes the same body, receives and checks the file, stores nothing, and
builds the command with an upload whose name is empty. An ``Action``
binds an upload by its content, not its name (``Action.arguments``), so
the twin and the act agree on the file. The twin's checks are
remembered for its caller (``receive_upload(remember=...)``): the act
does not check the same content again (ADR 0175).

The file's types, its size, where it is stored, and the body limit may
be decided per request: a callable given the request and the aggregate
the route reached (None on the collection), sync or async, in place of a
value. It runs once the reach is checked, before the body is read, on
the act and on its twin; the storage an alias names, and the directory,
are checked then too. A body limit so decided holds the body from then
on (``hold_body``), and the file to it too, whatever read the body
before.

A service that names a ``received`` parameter is given the file as it
was received (``ReceivedUpload``), its temporary file open from its
start, besides the stored upload in its command: it reads what the file
holds without reading the storage again. The route closes it once the
service is done, however it ends.
"""

import dataclasses
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import aclosing
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast
from urllib.parse import unquote

import anyio
from fastapi import Request, Response, params, status
from fastapi.routing import APIRoute
from starlette.datastructures import UploadFile

from domainkit.ddd.exceptions import PreconditionFailed
from domainkit.ddd.messages import Command
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.errors import error_responses
from domainkit.http.middleware import MAX_ROUTE_BODY_SIZE, hold_body
from domainkit.http.uploads import FileType, ReceivedUpload, receive_upload
from domainkit.storage.base import Storage, StoredUpload, new_name
from domainkit.storage.handler import storages

from ..conditional import PreconditionRequired, expected_versions
from . import reach
from .authorizing import BY_NAME, SUFFIX
from .commands import CommandRoute
from .context import RouteContext

if TYPE_CHECKING:
    from domainkit.ddd.uow import Retry
    from domainkit.http.access import Rule

    from . import ResourceRouter
    from .context import Returns, Service

FILENAME_HEADER: Final = "X-Filename"
"""The file's name when the body is the file: percent-encoded UTF-8."""
FORM_ROOM: Final = 64 * 1024
"""What a form adds to its file at most, in the default body limit."""
RECEIVED: Final = "domainkit.upload"
"""Where a request keeps the upload it received, once."""
STORED: Final = "domainkit.upload.stored"
"""Where a request keeps the upload it stored, for the command."""
FORGET_WITHIN: Final = 10.0
"""Seconds a failed request may take to delete what it stored."""
MAX_FORM_FIELDS: Final = 16
"""The form fields an upload's form may have besides its file."""
CHANGED: Final = "This was changed since you read it; read it again before changing it."
"""The detail of a 412, as a command route's."""
logger: Final = logging.getLogger(__name__)
HANDED: ContextVar[ReceivedUpload | None] = ContextVar(
    "domainkit.upload.handed", default=None
)
"""The file received for the request whose service runs, for a service that asks."""

type Decided[T] = Callable[[Request, Any], T | Awaitable[T]]
"""A bound of an upload decided per request: given the request and the aggregate
the route reached (None on the collection), it returns the bound, or an
awaitable of it."""


class Receiving(Protocol):
    """A service that is given the file received too (``received=``)."""

    def __call__(
        self,
        command: Any,  # noqa: ANN401 - as ``Service``.
        uow: Any,  # noqa: ANN401 - as ``Service``.
        /,
        *,
        received: ReceivedUpload,
    ) -> Awaitable[Any]:
        """Run ``command`` in ``uow``; ``received`` holds the file, open."""


@dataclasses.dataclass(frozen=True, slots=True)
class Bounds:
    """What one request's file is held to, and where it goes."""

    types: tuple[FileType, ...]
    max_size: int
    """The file's, never more than ``body_limit``."""
    storage: Storage
    """The storage itself: an alias is looked up when the bounds are decided."""
    directory: str
    body_limit: int | None = None
    """The request's, when the route decides it per request (``hold_body``)."""


class UploadRoute(CommandRoute):
    """``ResourceRouter.upload_route`` (see the module)."""

    def __init__(  # noqa: PLR0913
        self,
        router: "ResourceRouter",
        command: type[Command],
        *,
        service: "Service | Receiving",
        field: str,
        types: "Iterable[FileType] | Decided[Iterable[FileType]]",
        max_size: "int | Decided[int]",
        storage: "Storage | str | Decided[Storage | str]",
        directory: "str | Decided[str]",
        digest: str | None,
        returns: "Returns",
        method: Literal["POST", "PUT", "PATCH"],
        path: str,
        action: str | None,
        rule: "Rule | None",
        status_code: int | None,
        name: str | None,
        summary: str | None,
        provide: Mapping[str, Callable[..., object]] | None,
        body_limit: "int | Decided[int] | None",
        retry: "Retry | None",
        dependencies: Sequence[params.Depends] = (),
        before_rules: Sequence[params.Depends] = (),
    ) -> None:
        """Check the command against what gives its fields, and the upload's bounds.

        Raises:
            TypeError: The command lacks ``field`` or ``digest``, ``provide``
                gives one of them, or the command needs fields nothing gives;
                or ``max_size`` is decided per request and no ``body_limit``
                bounds it.
            ValueError: No type is taken, or ``max_size`` is not positive.
        """
        given = {field} if digest is None else {field, digest}
        if taken := sorted(given & set(provide or {})):
            raise TypeError(
                f"{', '.join(taken)} come from the uploaded file; provide= cannot "
                "give them."
            )
        self.field = field
        self.digest = digest
        self.types: Iterable[FileType] | Decided[Iterable[FileType]] = (
            types if callable(types) else _types(types)
        )
        if callable(max_size) and body_limit is None:
            raise TypeError(
                "max_size is decided per request: give body_limit=, the most a "
                "request may send, file and form; a number, or decided per "
                "request too."
            )
        default = None if callable(max_size) else _size(max_size) + FORM_ROOM
        self.max_size = max_size
        self.storage = storage
        self.directory = directory
        self.body_limit = body_limit if callable(body_limit) else None
        """The body limit, when it is decided per request."""
        declared = default if body_limit is None else body_limit
        if callable(declared):  # Until it is decided, held as a command route's.
            declared = None
        self.receives = _asks_received(service)
        """Whether the service is given the file received (``received=``)."""
        super().__init__(
            router,
            command,
            # The service itself, as the twin's rule reads it (``service_problem``).
            service=cast("Service", service),
            body=None,
            returns=returns,
            method=method,
            path=path,
            action=action,
            rule=rule,
            status_code=status_code,
            name=name,
            summary=summary,
            # The file gives these: named here so the command's fields check.
            provide={**(provide or {}), **dict.fromkeys(given, _from_the_file)},
            body_limit=declared,
            body_limit_per_request=self.body_limit is not None,
            retry=retry,
            dependencies=dependencies,
            before_rules=before_rules,
        )
        if self.body_limit is not None:
            self._check_rules_read_no_body()
        if self.receives:
            self.service = _handing(cast("Receiving", service))

    def _check_rules_read_no_body(self) -> None:
        """Refuse a rule that reads the body, on a route that decides its limit.

        The rule reads the body before the limit is decided, held to the
        limit of a command route without one: never to the decided one.

        Raises:
            ImproperlyConfigured: One of the route's rules reads the body.
        """
        for each in self.rules:
            rule = each() if isinstance(each, type) else each
            if rule.reads_the_body():
                raise ImproperlyConfigured(
                    f"{self.router.prefix}{self.spec.path} decides its body limit "
                    f"per request, but {type(rule).__name__} reads the body before "
                    "it is decided: give body_limit= a number, the most a request "
                    "may send."
                )

    def declare(self) -> None:
        """Add the route and its twin, and say in OpenAPI what body they take."""
        super().declare()
        prefix = f"{self.router.name_prefix}{self.spec.name}"
        for route in self.router.routes:
            if isinstance(route, APIRoute) and route.name in {
                prefix,
                f"{prefix}{SUFFIX}",
            }:
                self._document(route)

    async def build(self, context: RouteContext) -> Command:
        """The command, with the upload stored (or, on the twin, not) and its digest.

        Raises:
            UploadRefused: The file is empty, too large, not of a type
                taken, or refused by its type's check.
        """
        request = context.request
        stored: StoredUpload | None = request.scope.get(STORED)
        if stored is None:  # The twin: the reach first, then the file, kept nowhere.
            assert self.twin is not None  # noqa: S101 - only a twin builds before storing.
            aggregate = await self._reachable(context, self.twin.rules)
            bounds = await self.bounds(request, aggregate)
            received = await self.received(request, bounds)
            received.close()
            stored = received.unstored()
        else:
            received = await self.received(request)
        values: dict[str, object] = {self.field: stored}
        if self.digest is not None:
            values[self.digest] = received.sha256
        provided = {**context.provided, **values}
        return await super().build(dataclasses.replace(context, provided=provided))

    async def handle(self, context: RouteContext) -> Response:
        """Check the reach, receive and store the file, then run the command.

        What was stored is deleted when the request fails and its unit of
        work did not commit: a commit may have kept the file's name. A
        commit whose outcome is unknown (``UnitOfWork.in_doubt``: its
        answer lost, or cut off as it committed) may have kept it too: the
        file stays, and its storage and name are logged. A service that
        asks for the file received gets it open, and it is closed once the
        service is done, however it ends.

        Raises:
            NotFound: The aggregate is out of the caller's reach.
            PermissionDenied: A rule refuses the caller the aggregate.
            PreconditionFailed: ``If-Match`` names another version (412).
            PreconditionRequired: The resource requires ``If-Match`` (428).
        """
        aggregate = await self._reachable(context, self.rules, conditions=True)
        request = context.request
        bounds = await self.bounds(request, aggregate)
        received = await self.received(request, bounds)
        stored = await received.store(
            bounds.storage, directory=bounds.directory, keep=self.receives
        )
        handed = HANDED.set(received)
        try:
            request.scope[STORED] = stored
            uow = context.uow
            committed, in_doubt = uow.committed, uow.in_doubt
            try:
                return await super().handle(context)
            except BaseException:
                if uow.in_doubt != in_doubt:
                    logger.warning(
                        "Kept the upload %s on the %s storage: its request "
                        "failed as it committed, and the commit may have kept it",
                        stored.name,
                        _named(bounds.storage),
                    )
                elif uow.committed == committed:
                    await _forget(bounds.storage, stored.name)
                raise
        finally:
            HANDED.reset(handed)
            received.close()  # Kept for the service: closed however it ended.

    async def _reachable(
        self,
        context: RouteContext,
        rules: "tuple[Rule, ...]",
        *,
        conditions: bool = False,
    ) -> object:
        """Refuse the request before its body is read, as the act would.

        On one aggregate: within the caller's reach, the rules on it, and,
        with ``conditions``, ``If-Match``. A route on the collection has
        nothing to reach (its parent's checked already). The aggregate
        reached, or None on the collection.

        Raises:
            NotFound: The aggregate is out of the caller's reach.
            PermissionDenied: A rule refuses the caller the aggregate.
            PreconditionFailed: ``If-Match`` names another version.
            PreconditionRequired: The resource requires ``If-Match``.
        """
        if not self.detail:
            return None
        uow = context.uow
        async with uow:
            aggregate = await reach.load(self.router, uow, context.request, context.ids)
        await reach.check_object(context.request, aggregate, rules)
        if not conditions:
            return aggregate
        header = context.if_match
        versions = None if header is None else expected_versions(header)
        if versions is None and self.router.require_if_match:
            raise PreconditionRequired
        if versions is not None and getattr(aggregate, "version", None) not in versions:
            raise PreconditionFailed(CHANGED)
        return aggregate

    async def bounds(self, request: Request, aggregate: object) -> Bounds:
        """The request's types, size, storage, directory and body limit, decided now.

        Before the body is read: the storage an alias names is looked up,
        and the directory checked as a storage takes it. A file is held to
        the body limit decided, where that is less than its size.

        Raises:
            TypeError: A callable decided something of the wrong kind.
            ValueError: It decided no type, a size that is not a positive
                integer, a body limit out of its bounds, or a malformed
                directory.
            ImproperlyConfigured: ``STORAGES`` has no such alias.
        """
        types = await _decided(self.types, request, aggregate)
        max_size = _size(await _decided(self.max_size, request, aggregate))
        storage = await _decided(self.storage, request, aggregate)
        directory = await _decided(self.directory, request, aggregate)
        limit = None
        if self.body_limit is not None:
            limit = _body(await _decided(self.body_limit, request, aggregate))
            max_size = min(max_size, limit)  # The file is never more than its body.
        if not isinstance(storage, Storage | str) or not isinstance(directory, str):
            raise TypeError(
                "An upload route's storage is a Storage or an alias, and its "
                f"directory text: not {storage!r}, {directory!r}."
            )
        new_name(directory)  # Malformed: ValueError, before a byte is read.
        target = storages[storage] if isinstance(storage, str) else storage
        return Bounds(_types(types), max_size, target, directory, limit)

    async def received(
        self, request: Request, bounds: Bounds | None = None
    ) -> ReceivedUpload:
        """The request's file, received and checked once, as ``bounds`` say.

        Raises:
            UploadRefused: It is empty, too large, not of a type taken, or
                refused by its type's check.
        """
        found = request.scope.get(RECEIVED)
        if found is None:
            assert bounds is not None  # noqa: S101 - the first receive decides them.
            if bounds.body_limit is not None:
                hold_body(request.scope, bounds.body_limit)
            found = request.scope[RECEIVED] = await self._receive(request, bounds)
        return cast("ReceivedUpload", found)

    async def _receive(self, request: Request, bounds: Bounds) -> ReceivedUpload:
        """Receive the body as the file, or the form's part named after the field.

        On a route that asks for an authorization, the checks' verdict is
        remembered for its caller: the twin's is the act's.
        """
        remember = self._remembered_as(request)
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind == "multipart/form-data":
            async with request.form(max_files=1, max_fields=MAX_FORM_FIELDS) as form:
                part = form.get(self.field)
                return await receive_upload(
                    part if isinstance(part, UploadFile) else b"",
                    types=bounds.types,
                    max_size=bounds.max_size,
                    remember=remember,
                )
        header = request.headers.get(FILENAME_HEADER)
        async with aclosing(request.stream()) as body:
            return await receive_upload(
                body,
                types=bounds.types,
                max_size=bounds.max_size,
                filename=None if header is None else unquote(header),
                remember=remember,
            )

    def _remembered_as(self, request: Request) -> str | None:
        """The name the twin and the act remember a file under, or None.

        The route's and its caller's: a verdict is never another caller's,
        whose files a quicker answer would tell of. None on a route with no
        twin, or for a caller with no id.
        """
        caller = getattr(request.scope.get("user"), "id", None)
        if self.twin is None or caller is None:
            return None
        return f"{self.router.name_prefix}{self.spec.name}\x1f{caller}"

    def _document(self, route: APIRoute) -> None:
        """Document the body the route takes: the file, or a form holding it."""
        extra = dict(route.openapi_extra or {})
        binary = {"type": "string", "format": "binary"}
        extra["requestBody"] = {
            "required": True,
            "description": (
                f"The file: the body itself, its name in {FILENAME_HEADER} "
                f"(percent-encoded UTF-8); or a form's {self.field!r} part. Up to "
                f"{_shown(self.max_size)}, of the types the route takes, by content."
            ),
            "content": {
                "application/octet-stream": {"schema": binary},
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": [self.field],
                        "properties": {self.field: binary},
                    }
                },
            },
        }
        extra["parameters"] = [
            *extra.get("parameters", []),
            {
                "name": FILENAME_HEADER,
                "in": "header",
                "required": False,
                "description": "The file's name, percent-encoded UTF-8.",
                "schema": {"type": "string"},
            },
        ]
        route.openapi_extra = extra
        documented = error_responses(
            status.HTTP_413_CONTENT_TOO_LARGE,
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
        route.responses = {**documented, **route.responses}


async def _from_the_file() -> None:
    """Nothing: the file gives the field, once it is received (``build``)."""


async def _decided[T](
    value: "T | Decided[T]", request: Request, aggregate: object
) -> T:
    """``value``, or what it decides for ``request`` and ``aggregate``, awaited."""
    if not callable(value):
        return value
    found = value(request, aggregate)
    return cast("T", await found if inspect.isawaitable(found) else found)


def _types(types: Iterable[FileType]) -> tuple[FileType, ...]:
    """``types``, as a tuple.

    Raises:
        ValueError: There are none.
    """
    found = tuple(types)
    if not found:
        raise ValueError("An upload route needs the types it accepts.")
    return found


def _size(max_size: object) -> int:
    """``max_size``, a whole number of bytes, 1 or more.

    Raises:
        ValueError: It is not: a boolean, a float, or under 1.
    """
    if not isinstance(max_size, int) or isinstance(max_size, bool) or max_size < 1:
        raise ValueError(f"max_size is 1 byte or more, not {max_size!r}.")
    return max_size


def _body(limit: object) -> int:
    """``limit``, a body limit decided per request: whole bytes, within a route's.

    Raises:
        ValueError: It is not a whole number of bytes from 1 to
            ``MAX_ROUTE_BODY_SIZE``.
    """
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_ROUTE_BODY_SIZE
    ):
        raise ValueError(
            f"A body limit is 1 to {MAX_ROUTE_BODY_SIZE} bytes, not {limit!r}."
        )
    return limit


def _asks_received(service: object) -> bool:
    """Whether ``service`` names a ``received`` parameter it can be given by name."""
    try:
        parameters = inspect.signature(service).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):  # No signature to read: given the command alone.
        return False
    found = parameters.get("received")
    return found is not None and found.kind in BY_NAME


def _handing(service: "Receiving") -> "Service":
    """``service``, given the file the request received, from its start."""

    async def handed(command: Command, uow: object) -> object:
        received = HANDED.get()
        assert received is not None  # noqa: S101 - set while the service runs.
        received.file.seek(0)  # From its start: a retry reads it again.
        return await service(command, uow, received=received)

    return handed


def _shown(max_size: "int | Decided[int]") -> str:
    """The route's size bound, as OpenAPI says it."""
    return "the size decided per request" if callable(max_size) else f"{max_size} bytes"


async def _forget(storage: Storage | str, name: str) -> None:
    """Delete what a failed request stored; log, never raise, if it cannot.

    The lines name the file and its storage: a name is random, not a secret
    (ADR 0040), and it is what an operator deletes by hand.
    """
    target = storages[storage] if isinstance(storage, str) else storage
    with anyio.CancelScope(shield=True), anyio.move_on_after(FORGET_WITHIN) as scope:
        try:
            await target.delete(name)
        except Exception:
            logger.exception(
                "The upload %s of a failed request could not be deleted from "
                "the %s storage",
                name,
                _named(storage),
            )
    if scope.cancelled_caught:
        logger.warning(
            "The upload %s of a failed request took too long to delete from "
            "the %s storage",
            name,
            _named(storage),
        )


def _named(storage: Storage | str) -> str:
    """How logs name ``storage``: its ``STORAGES`` alias, or its class."""
    return repr(storage) if isinstance(storage, str) else type(storage).__name__
