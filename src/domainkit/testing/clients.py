"""Test clients for an ASGI application: in another thread, or in this loop.

``TestClient`` is Starlette's: the application runs in a thread of its own,
with its own event loop, and tests call it synchronously. ``AsyncTestClient``
is an ``httpx2.AsyncClient`` that runs the application in the calling task,
so async tests await it, and share its event loop and database connections.
Both run the application's lifespan while the client is entered, and in
both, requests see the context variables of the code that sends them: a
``freeze_time`` block around a request freezes the application's clock too.

``user_token`` stores a user with permissions and returns an access token,
as the ``client_for`` fixtures do.
"""

import asyncio
import inspect
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Generator,
    Mapping,
    MutableMapping,
)
from concurrent.futures import Future
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
)
from contextvars import Context, copy_context
from functools import partial
from types import TracebackType
from typing import Any, Final, Self, cast
from uuid import uuid4

import httpx2
from anyio.from_thread import BlockingPortal
from fastapi.testclient import TestClient as StarletteTestClient
from starlette.types import ASGIApp, Message, Receive, Send

TEST_SERVER: Final = "http://testserver"
TEST_CLIENT: Final = ("testclient", 50000)
"""The client address both clients send, as Starlette's does."""


class TestClient(StarletteTestClient):
    """Starlette's ``TestClient``; the application sees the caller's context.

    Each request and WebSocket session starts, in the application's thread,
    with the values the sending thread's context variables hold.
    """

    __test__ = False

    @contextmanager
    def _portal_factory(self) -> Generator[BlockingPortal]:
        """Hand the transport a portal that carries the caller's context."""
        with super()._portal_factory() as portal:
            yield cast("BlockingPortal", CarryingPortal(portal))

    def another(self, headers: Mapping[str, str]) -> "TestClient":
        """Return a client of the same application that sends ``headers``.

        It shares this client's event loop and lifespan, so its requests use
        the same database connections. Enter this client first.
        """
        client = TestClient(self.app, headers=dict(headers))
        client.portal = self._entered()
        client.app_state.update(self.app_state)
        return client

    def authenticated(self, *permissions: str, **fields: Any) -> "TestClient":  # noqa: ANN401 - the user model's fields.
        """Return a client that authenticates as a new user (see ``user_token``).

        The user is stored on this client's event loop. Enter this client first.
        """
        portal = CarryingPortal(self._entered())
        token = portal.call(partial(user_token, *permissions, **fields))
        return self.another({"Authorization": f"Bearer {token}"})

    def _entered(self) -> BlockingPortal:
        """This client's portal; refuse a client that is not entered.

        Without it, each request would start an event loop of its own, with
        database connections nothing closes.
        """
        if self.portal is None:
            raise RuntimeError("Enter the client first: with TestClient(app) as ...")
        return self.portal


class CarryingPortal:
    """A blocking portal whose calls start with the caller's context values.

    Every method that starts work in the event loop from another thread
    (``call``, ``start_task_soon``, ``start_task`` and
    ``wrap_async_context_manager``, the whole of anyio's) runs it with a
    copy of the calling thread's context. Starlette's ``TestClient`` runs
    the application through these; which ones depends on its version.
    Anything else is the wrapped portal's own.
    """

    def __init__(self, portal: BlockingPortal) -> None:
        """Wrap ``portal``."""
        self.portal = portal

    def call(self, func: Callable[..., Any], *args: object) -> Any:  # noqa: ANN401 - what func returns.
        """Run ``func(*args)`` in the event loop; return what it returns."""
        return self.portal.call(_carried, copy_context(), func, *args)

    def start_task_soon(
        self, func: Callable[..., Any], *args: object, name: object = None
    ) -> Future[Any]:
        """Start ``func(*args)`` in the event loop; return a future of its result.

        Cancelling the future cancels the task, as the wrapped portal's does.
        """
        return self.portal.start_task_soon(
            _carried, copy_context(), func, *args, name=name
        )

    def start_task(
        self, func: Callable[..., Awaitable[Any]], *args: object, name: object = None
    ) -> tuple[Future[Any], Any]:
        """Start ``func(*args, task_status=...)`` and wait until it has started."""
        return self.portal.start_task(
            _carried_task, copy_context(), func, *args, name=name
        )

    def wrap_async_context_manager[T](
        self, manager: AbstractAsyncContextManager[T]
    ) -> AbstractContextManager[T]:
        """Return a context manager that enters and exits ``manager`` in the loop."""
        return self.portal.wrap_async_context_manager(
            _carried_manager(copy_context(), manager)
        )

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - the portal's attribute.
        """Read any other attribute from the wrapped portal."""
        return getattr(self.portal, name)


class AsyncTestClient(httpx2.AsyncClient):
    """An ``httpx2.AsyncClient`` that serves an ASGI application in this task.

    ``async with`` runs the application's lifespan around the requests and
    closes the client after. Requests run in the task that sends them, so
    they see its context variables (a frozen clock) and its event loop's
    database connections. Application errors are raised, as ``TestClient``
    raises them. WebSockets need ``TestClient``.
    """

    __test__ = False

    def __init__(
        self,
        app: ASGIApp,
        *,
        base_url: str = TEST_SERVER,
        headers: Mapping[str, str] | None = None,
        client: tuple[str, int] = TEST_CLIENT,
        lifespan: bool = True,
    ) -> None:
        """Serve ``app``; ``lifespan=False`` skips its startup and shutdown."""
        self.app = app
        self.app_state: dict[str, Any] = {}
        """The lifespan's state; each request gets a copy, as servers do."""
        self.lifespan = Lifespan(app, self.app_state) if lifespan else None
        super().__init__(
            transport=httpx2.ASGITransport(
                app=_WithState(app, self.app_state), client=client
            ),
            base_url=base_url,
            headers={"user-agent": "testclient", **(headers or {})},
            follow_redirects=True,
        )

    async def __aenter__(self) -> Self:
        """Start the application's lifespan, then the client."""
        if self.lifespan is not None:
            await self.lifespan.startup()
        await super().__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        """Close the client, then shut the application down."""
        await self.aclose()
        if self.lifespan is not None:
            await self.lifespan.shutdown()

    def another(self, headers: Mapping[str, str]) -> "AsyncTestClient":
        """Return a client of the same application that sends ``headers``.

        It shares this client's lifespan instead of running its own; close
        it with ``aclose()``.
        """
        client = AsyncTestClient(self.app, headers=headers, lifespan=False)
        client.app_state.update(self.app_state)
        return client

    async def authenticated(
        self,
        *permissions: str,
        **fields: Any,  # noqa: ANN401 - the user model's fields.
    ) -> "AsyncTestClient":
        """Return a client that authenticates as a new user (see ``user_token``)."""
        token = await user_token(*permissions, **fields)
        return self.another({"Authorization": f"Bearer {token}"})


class Lifespan:
    """Run an ASGI application's lifespan in the running event loop."""

    _task: "asyncio.Task[None]"

    def __init__(self, app: ASGIApp, state: dict[str, Any]) -> None:
        """Run ``app``'s lifespan with ``state`` as its state."""
        self.app = app
        self.state = state
        self._to_app: asyncio.Queue[Message] = asyncio.Queue()
        self._from_app: asyncio.Queue[Message | None] = asyncio.Queue()

    async def startup(self) -> None:
        """Start the application; raise what made its startup fail."""
        self._task = asyncio.create_task(self._run())
        await self._exchange("startup")

    async def shutdown(self) -> None:
        """Stop the application; raise what made its shutdown fail."""
        await self._exchange("shutdown")
        await self._task

    async def _run(self) -> None:
        """Serve the lifespan scope; tell the other side when the app is done."""
        scope = {"type": "lifespan", "state": self.state}
        try:
            await self.app(scope, self._to_app.get, self._from_app.put)
        finally:
            self._from_app.put_nowait(None)

    async def _exchange(self, phase: str) -> None:
        """Send ``lifespan.<phase>`` and wait for the answer."""
        await self._to_app.put({"type": f"lifespan.{phase}"})
        message = await self._from_app.get()
        if message is not None and message["type"] == f"lifespan.{phase}.complete":
            return
        await self._task  # Raises what the application raised, if it did.
        reason = "it stopped" if message is None else message.get("message") or ""
        raise RuntimeError(f"The application's lifespan {phase} failed: {reason}")


class _WithState:
    """Give each request a copy of the lifespan's state, as servers do."""

    def __init__(self, app: ASGIApp, state: dict[str, Any]) -> None:
        """Wrap ``app``."""
        self.app = app
        self.state = state

    async def __call__(
        self, scope: MutableMapping[str, Any], receive: Receive, send: Send
    ) -> None:
        """Serve the request with the state in its scope."""
        await self.app({**scope, "state": self.state.copy()}, receive, send)


async def user_token(*permissions: str, **fields: Any) -> str:  # noqa: ANN401 - the user model's fields.
    """Store a new user granted ``permissions``; return an access token for them.

    Keyword arguments set the user's fields (``is_staff=True``); the
    username is unique unless given. The token comes from the first
    ``AUTH.AUTHENTICATION_CLASSES`` class, as a login's would. Needs
    ``domainkit.contrib.auth``.
    """
    from domainkit.contrib.auth import AuthUnitOfWork, get_user_model
    from domainkit.contrib.auth.authentication import get_authenticators

    username = fields.pop(get_user_model().USERNAME_FIELD, f"user-{uuid4().hex}")
    async with AuthUnitOfWork() as uow:
        user = await uow.users.create_user(username, **fields)
        user.grant(*await uow.permissions.get_many(permissions))
        issued = await get_authenticators()[0].issue(uow, user)
        await uow.commit()
    return issued.access_token


def _adopt(context: Context) -> None:
    """Give the running task's context the values ``context`` holds."""
    for variable, value in context.items():
        variable.set(value)


async def _carried(context: Context, func: Callable[..., Any], *args: object) -> Any:  # noqa: ANN401 - what func returns.
    """Call ``func(*args)`` with ``context``'s values, awaiting what it returns."""
    _adopt(context)
    result = func(*args)
    if inspect.isawaitable(result):
        return await result
    return result


@asynccontextmanager
async def _carried_manager[T](
    context: Context, manager: AbstractAsyncContextManager[T]
) -> AsyncGenerator[T]:
    """Enter and exit ``manager`` with ``context``'s values."""
    _adopt(context)
    async with manager as value:
        yield value


async def _carried_task(
    context: Context,
    func: Callable[..., Awaitable[Any]],
    *args: object,
    task_status: Any,  # noqa: ANN401 - anyio's TaskStatus.
) -> Any:  # noqa: ANN401 - what func returns.
    """Run ``func(*args, task_status=...)`` with ``context``'s values."""
    _adopt(context)
    return await func(*args, task_status=task_status)
