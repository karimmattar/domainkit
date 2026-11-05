"""Hourly purge of ended sign-in requests and expired assertion ids."""

from datetime import timedelta
from typing import Final

from domainkit.contrib.auth.integration import utc_now
from domainkit.contrib.messaging import periodic

from .stores import CibaUnitOfWork

KEPT: Final = timedelta(days=1)
"""How long an ended request stays, for the phone's history and support."""


@periodic(every=timedelta(hours=1), name="domainkit.ciba.purge")
async def purge() -> None:
    """Forget requests a day after they ended, and assertion ids once expired."""
    now = utc_now()
    async with CibaUnitOfWork() as uow:
        await uow.requests.purge(now - KEPT)
        await uow.assertions.purge(now)
        await uow.commit()
