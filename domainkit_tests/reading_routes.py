"""Routes that read gadgets in each way a read-only unit of work reads.

Tests serve them through ``AsyncTestClient``, in the test's own event loop,
so the test can close the pool's connections between requests.
"""

from fastapi import APIRouter
from sqlalchemy import text

from domainkit.db import Reads
from domainkit_tests.gadgets import GadgetsUnitOfWork

router = APIRouter()


@router.get("/gadgets")
async def gadget_names(reads: Reads | None = None) -> list[str]:
    """The names of every gadget: read as ``reads`` says, or in a writable one."""
    async with GadgetsUnitOfWork(reads=reads) as uow:
        return sorted(gadget.name for gadget in await uow.gadgets.filter())


@router.get("/slow")
async def slow(reads: Reads) -> dict[str, str]:
    """A read that takes 30 seconds, unless something cuts it off."""
    async with GadgetsUnitOfWork(reads=reads) as uow:
        await uow.session.execute(text("SELECT pg_sleep(30)"))
    return {"slept": "30s"}
