"""Hourly purge of the signed requests no longer within the window."""

from datetime import timedelta
from typing import Final

from domainkit.apps import registry
from domainkit.contrib.auth.integration import utc_now

from .settings import DevicesSettings
from .stores import DevicesUnitOfWork

__all__ = ["purge_signatures", "register_purge"]

PURGE_TASK: Final = "domainkit.devices.purge_signatures"


async def purge_signatures() -> int:
    """Forget the signed requests too old to be sent again; how many.

    A device forgets its own as it is used (``DeviceStore.touch``): this
    clears those of devices that stopped.
    """
    window = registry.settings(DevicesSettings).SIGNATURE_WINDOW
    before = int((utc_now() - window).timestamp() * 1000)
    async with DevicesUnitOfWork() as uow:
        purged = await uow.devices.purge_signatures(before)
        await uow.commit()
    return purged


def register_purge() -> None:
    """Run the purge every hour (needs messaging)."""
    from domainkit.contrib.messaging import periodic

    periodic(every=timedelta(hours=1), name=PURGE_TASK)(purge_signatures)
