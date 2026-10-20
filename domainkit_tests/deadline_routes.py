"""Routes that take their time: for request deadlines and load shedding.

Tests serve them through ``AsyncTestClient``, in their own event loop, so a
route can wait on an ``asyncio.Event`` a test holds (``gates``) and tell the
test it has started (``entered``).
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import insert, text, update

from domainkit import clock, deadlines
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


async def forever() -> None:
    """Wait for an event nobody sets: only a deadline ends it."""
    await asyncio.Event().wait()


@router.get("/hang")
async def hang() -> None:
    """Never answer: the deadline cuts it off."""
    seen.append(deadlines.remaining())
    await forever()


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
