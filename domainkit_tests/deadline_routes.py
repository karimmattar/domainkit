"""Routes that take their time: for request deadlines and load shedding.

Tests serve them through ``AsyncTestClient``, in their own event loop, so a
route can wait on an ``asyncio.Event`` a test holds (``gates``) and tell the
test it has started (``entered``).
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import insert, text, update
from sqlalchemy.exc import DBAPIError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from domainkit import clock, deadlines
from domainkit.ddd import AggregateRoot, DomainEvent, EventBus, UnitOfWork
from domainkit.http.deadlines import RequestTimeout
from domainkit.http.health import router as health_router
from domainkit_tests.sample_routes import PaymentsUnitOfWork, payments

router = APIRouter()
router.include_router(health_router)


class Counter:
    """A count tests wait on without polling: ``await counter.reach(2)``."""

    def __init__(self) -> None:
        """Start at 0."""
        self.value = 0
        self._changed: asyncio.Event | None = None

    def add(self, change: int = 1) -> None:
        """Change the count, waking whoever waits on it."""
        self.value += change
        if self._changed is not None:
            self._changed.set()

    async def reach(self, value: int) -> None:
        """Wait until the count is ``value``."""
        while self.value != value:
            self._changed = asyncio.Event()
            await self._changed.wait()


arrivals = Counter()
"""Requests that reached ``/busy``."""
gates: dict[str, asyncio.Event] = {}
"""``/busy?gate=...`` waits until the test sets its event."""
seen: list[float | None] = []
"""What ``deadlines.remaining()`` said in the routes that report it."""
hang_payments: list[bool] = [True]
"""Whether ``/slow-pay`` hangs after writing (else it commits)."""
paying: list[deadlines.Deadline | None] = []
"""The deadline of each ``/slow-pay`` that wrote and hangs, for the test to end."""
swallowing: list[deadlines.Deadline | None] = []
"""The deadline of each ``/swallow`` waiting, for the test to end."""
told: list[object] = []
"""What the handlers of ``/pay-and-tell``'s event did, in order."""
telling: list[deadlines.Deadline] = []
"""The deadline of the ``/pay-and-tell`` that committed, for its slow handler."""


async def forever() -> None:
    """Wait for an event nobody sets: only a deadline ends it."""
    await asyncio.Event().wait()


@router.get("/hang")
async def hang() -> None:
    """Never answer: the deadline cuts it off."""
    seen.append(deadlines.remaining())
    await forever()


@router.post("/echo")
async def echo(body: dict[str, Any]) -> dict[str, Any]:
    """The JSON body it was sent, once all of it arrived."""
    return body


@router.get("/remaining")
async def remaining() -> dict[str, float | None]:
    """The seconds left before the request's deadline."""
    return {"remaining": deadlines.remaining()}


@router.get("/longer", dependencies=[Depends(RequestTimeout(60.0))])
async def longer() -> dict[str, float | None]:
    """A route given a minute, longer than ``REQUEST_TIMEOUT`` in the test."""
    return {"remaining": deadlines.remaining()}


@router.get("/unbounded", dependencies=[Depends(RequestTimeout(None))])
async def unbounded() -> dict[str, float | None]:
    """A route without a deadline."""
    return {"remaining": deadlines.remaining()}


@router.get("/shorter", dependencies=[Depends(RequestTimeout(0.05))])
async def shorter() -> None:
    """A route given less than ``REQUEST_TIMEOUT``: cut off sooner."""
    await forever()


@router.get("/stream")
async def stream() -> StreamingResponse:
    """A response that keeps sending once it has started, without a deadline."""

    async def chunks() -> AsyncIterator[bytes]:
        seen.append(deadlines.remaining())  # The response has started.
        yield b"started;"
        seen.append(deadlines.remaining())
        yield b"finished"

    return StreamingResponse(chunks(), media_type="text/plain")


@router.get("/late")
async def late() -> StreamingResponse:
    """A response that runs out of time after it started."""

    async def chunks() -> AsyncIterator[bytes]:
        yield b"started;"
        raise deadlines.DeadlineExceeded("A deadline inside ran out.")

    return StreamingResponse(chunks(), media_type="text/plain")


@router.get("/spent")
async def spent(seconds: float) -> None:
    """Spend ``seconds`` without awaiting (on a frozen clock), then wait."""
    frozen = clock.frozen()
    assert frozen is not None
    frozen.tick(seconds)
    await deadlines.within(forever())


@router.get("/timeout")
async def own_timeout() -> None:
    """A timeout of the route's own, not the request's deadline."""
    async with asyncio.timeout(0.01):
        await forever()


@router.get("/swallow")
async def swallow() -> dict[str, bool]:
    """Catch the deadline's cancellation and answer anyway."""
    try:
        swallowing.append(deadlines.current_deadline())
        arrivals.add()
        await forever()
    except asyncio.CancelledError:
        return {"swallowed": True}
    return {"swallowed": False}  # pragma: no cover - forever() never returns.


@router.get("/busy")
async def busy(gate: str = "default") -> dict[str, str]:
    """Hold a slot until the test opens ``gate``."""
    arrivals.add()
    await gates.setdefault(gate, asyncio.Event()).wait()
    return {"gate": gate}


@router.post("/slow-pay", status_code=201)
async def slow_pay() -> dict[str, Any]:
    """Write a payment, then hang before committing (unless told not to)."""
    async with PaymentsUnitOfWork() as uow:
        await uow.session.execute(insert(payments))
        await uow.session.flush()
        if hang_payments[0]:
            paying.append(deadlines.current_deadline())
            arrivals.add()
            await forever()
        await uow.commit()
    return {"paid": True}


@router.post("/locked-pay")
async def locked_pay() -> None:
    """Change every payment: waits while another transaction holds one."""
    async with PaymentsUnitOfWork() as uow:
        await uow.session.execute(update(payments).values(id=payments.c.id))
        await uow.commit()


@router.get("/statement-timeout")
async def statement_timeout() -> dict[str, str]:
    """The ``statement_timeout`` a transaction of this request runs with."""
    async with PaymentsUnitOfWork(readonly=True) as uow:
        shown = await uow.session.scalar(text("SHOW statement_timeout"))
    return {"statement_timeout": str(shown)}


@router.get(
    "/month-long-statement-timeout",
    dependencies=[Depends(RequestTimeout(3_000_000.0))],
)
async def month_long_statement_timeout() -> dict[str, str]:
    """``/statement-timeout`` under a deadline longer than PostgreSQL takes."""
    return await statement_timeout()


class Payment(AggregateRoot[int]):
    """A payment ``/pay-and-tell`` records, for the event it publishes."""


class Paid(DomainEvent):
    """A payment was recorded."""


tellers = EventBus()
"""The bus of ``/pay-and-tell``: a slow handler, then another."""


async def tell_slowly(event: Paid) -> None:
    """Run past the request's deadline, then note what is left of it.

    It waits until 50 ms after the moment the deadline was set for, however
    long the request took to commit.
    """
    told.append("slow started")
    await asyncio.sleep(past(telling[0]))
    told.append(("slow finished", deadlines.remaining()))


def past(deadline: deadlines.Deadline) -> float:
    """Seconds from now until 50 ms past the moment ``deadline`` was set for."""
    assert deadline.seconds is not None
    return max(deadline.started + deadline.seconds - clock.monotonic(), 0.0) + 0.05


async def tell_next(event: Paid) -> None:
    """Run after the slow handler."""
    told.append("next")


tellers.subscribe(Paid, tell_slowly)
tellers.subscribe(Paid, tell_next)


class PaysAndTells(PaymentsUnitOfWork):
    """Records a payment, and publishes ``Paid`` once it is committed."""

    def _seen(self) -> list[Any]:
        payment = Payment(id=1)
        payment.record(Paid())
        return [payment]


@router.post("/pay-and-tell", status_code=201)
async def pay_and_tell(after: float = 0.0, in_task: bool = False) -> dict[str, Any]:
    """Commit a payment whose handlers outlast the deadline, then wait ``after``.

    ``in_task`` commits in a task of its own (``asyncio.gather``), which
    shares the request's deadline without enforcing it.
    """
    deadline = deadlines.current_deadline()
    assert deadline is not None
    telling[:] = [deadline]

    async def commit() -> None:
        async with PaysAndTells(events=tellers) as uow:
            await uow.session.execute(insert(payments))
            await uow.commit()

    if in_task:
        await asyncio.gather(commit())
    else:
        await commit()
    await asyncio.sleep(after)
    return {"paid": True, "remaining": deadlines.remaining()}


class LosesItsCommitsAnswer(PaymentsUnitOfWork):
    """Commits, then loses the connection before the answer comes: in doubt."""

    async def _commit(self) -> None:
        await super()._commit()
        raise DBAPIError(
            "COMMIT",
            {},
            ConnectionResetError("the connection was lost"),
            connection_invalidated=True,
        )


class CommitsForEver(PaymentsUnitOfWork):
    """Sends its ``COMMIT``, whose answer never comes: the deadline cuts it."""

    async def _commit(self) -> None:
        await forever()


@router.post("/pay-unanswered", status_code=201)
async def pay_unanswered(lost: bool = True) -> dict[str, bool]:
    """Commit, and lose the answer (``lost``), or wait for it until cut off."""
    unit = LosesItsCommitsAnswer if lost else CommitsForEver
    async with unit() as uow:
        await uow.session.execute(insert(payments))
        await uow.commit()
    return {"paid": True}  # pragma: no cover - the commit raised.


class CommitsInMemoryForEver(UnitOfWork):
    """Sends its commit, whose answer never comes; it holds no connection."""

    async def _commit(self) -> None:
        await forever()

    async def _rollback(self) -> None:
        """Nothing to undo."""


@router.post("/commit-unanswered", status_code=201)
async def commit_unanswered(in_task: bool = False) -> dict[str, bool]:
    """Commit until the deadline cuts the commit off: in doubt.

    ``in_task`` commits in a task group's task, which shares the request's
    deadline without enforcing it.
    """

    async def commit() -> None:
        async with CommitsInMemoryForEver() as uow:
            await uow.commit()

    if in_task:
        async with asyncio.TaskGroup() as group:
            group.create_task(commit())
    else:
        await commit()
    return {"paid": True}  # pragma: no cover - the commit never returns.


class InATask(BaseHTTPMiddleware):
    """Starlette's ``BaseHTTPMiddleware``: it serves the route in a task of its own."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Pass the request on."""
        return await call_next(request)
