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
the twin and the act agree on the file.
"""

import dataclasses
import logging
from collections.abc import Callable, Iterable, Mapping
from contextlib import aclosing
from typing import TYPE_CHECKING, Final, Literal, cast
from urllib.parse import unquote

import anyio
from fastapi import Request, Response, status
from fastapi.routing import APIRoute
from starlette.datastructures import UploadFile

from domainkit.ddd.exceptions import PreconditionFailed
from domainkit.ddd.messages import Command
from domainkit.http.errors import error_responses
from domainkit.http.uploads import FileType, ReceivedUpload, receive_upload
from domainkit.storage.base import Storage, StoredUpload
from domainkit.storage.handler import storages

from ..conditional import PreconditionRequired, expected_versions
from . import reach
from .authorizing import SUFFIX
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


class UploadRoute(CommandRoute):
    """``ResourceRouter.upload_route`` (see the module)."""

    def __init__(
        self,
        router: "ResourceRouter",
        command: type[Command],
        *,
        service: "Service",
        field: str,
        types: Iterable[FileType],
        max_size: int,
        storage: Storage | str,
        directory: str,
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
        body_limit: int | None,
        retry: "Retry | None",
    ) -> None:
        """Check the command against what gives its fields, and the upload's bounds.

        Raises:
            TypeError: The command lacks ``field`` or ``digest``, ``provide``
                gives one of them, or the command needs fields nothing gives.
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
        self.types = tuple(types)
        if not self.types:
            raise ValueError("An upload route needs the types it accepts.")
        if max_size < 1:
            raise ValueError(f"max_size is 1 byte or more, not {max_size!r}.")
        self.max_size = max_size
        self.storage = storage
        self.directory = directory
        super().__init__(
            router,
            command,
            service=service,
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
            body_limit=max_size + FORM_ROOM if body_limit is None else body_limit,
            retry=retry,
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
            await self._reachable(context, self.twin.rules)
            received = await self.received(request)
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
        work did not commit: a commit may have kept the file's name.

        Raises:
            NotFound: The aggregate is out of the caller's reach.
            PermissionDenied: A rule refuses the caller the aggregate.
            PreconditionFailed: ``If-Match`` names another version (412).
            PreconditionRequired: The resource requires ``If-Match`` (428).
        """
        await self._reachable(context, self.rules, conditions=True)
        request = context.request
        received = await self.received(request)
        stored = await received.store(self.storage, directory=self.directory)
        request.scope[STORED] = stored
        committed = context.uow.committed
        try:
            return await super().handle(context)
        except BaseException:
            if context.uow.committed == committed:
                await _forget(self.storage, stored.name)
            raise

    async def _reachable(
        self,
        context: RouteContext,
        rules: "tuple[Rule, ...]",
        *,
        conditions: bool = False,
    ) -> None:
        """Refuse the request before its body is read, as the act would.

        On one aggregate: within the caller's reach, the rules on it, and,
        with ``conditions``, ``If-Match``. A route on the collection has
        nothing to reach (its parent's checked already).

        Raises:
            NotFound: The aggregate is out of the caller's reach.
            PermissionDenied: A rule refuses the caller the aggregate.
            PreconditionFailed: ``If-Match`` names another version.
            PreconditionRequired: The resource requires ``If-Match``.
        """
        if not self.detail:
            return
        uow = context.uow
        async with uow:
            aggregate = await reach.load(self.router, uow, context.request, context.ids)
        await reach.check_object(context.request, aggregate, rules)
        if not conditions:
            return
        header = context.if_match
        versions = None if header is None else expected_versions(header)
        if versions is None and self.router.require_if_match:
            raise PreconditionRequired
        if versions is not None and getattr(aggregate, "version", None) not in versions:
            raise PreconditionFailed(CHANGED)

    async def received(self, request: Request) -> ReceivedUpload:
        """The request's file, received and checked once.

        Raises:
            UploadRefused: It is empty, too large, not of a type taken, or
                refused by its type's check.
        """
        found = request.scope.get(RECEIVED)
        if found is None:
            found = request.scope[RECEIVED] = await self._receive(request)
        return cast("ReceivedUpload", found)

    async def _receive(self, request: Request) -> ReceivedUpload:
        """Receive the body as the file, or the form's part named after the field."""
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind == "multipart/form-data":
            async with request.form(max_files=1, max_fields=MAX_FORM_FIELDS) as form:
                part = form.get(self.field)
                return await receive_upload(
                    part if isinstance(part, UploadFile) else b"",
                    types=self.types,
                    max_size=self.max_size,
                )
        header = request.headers.get(FILENAME_HEADER)
        async with aclosing(request.stream()) as body:
            return await receive_upload(
                body,
                types=self.types,
                max_size=self.max_size,
                filename=None if header is None else unquote(header),
            )

    def _document(self, route: APIRoute) -> None:
        """Document the body the route takes: the file, or a form holding it."""
        extra = dict(route.openapi_extra or {})
        binary = {"type": "string", "format": "binary"}
        extra["requestBody"] = {
            "required": True,
            "description": (
                f"The file: the body itself, its name in {FILENAME_HEADER} "
                f"(percent-encoded UTF-8); or a form's {self.field!r} part. Up to "
                f"{self.max_size} bytes, of the types the route takes, by content."
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


async def _forget(storage: Storage | str, name: str) -> None:
    """Delete what a failed request stored; log, never raise, if it cannot."""
    target = storages[storage] if isinstance(storage, str) else storage
    with anyio.CancelScope(shield=True), anyio.move_on_after(FORGET_WITHIN) as scope:
        try:
            await target.delete(name)
        except Exception:
            logger.exception("The upload of a failed request could not be deleted")
    if scope.cancelled_caught:
        logger.warning("The upload of a failed request took too long to delete")
