"""The devices app's tasks: the signed requests' purge, and the bound-device notice."""

from datetime import timedelta
from typing import Final
from uuid import UUID

from domainkit.apps import registry
from domainkit.contrib.auth.integration import mail_user, utc_now
from domainkit.contrib.auth.models import UserId

from .settings import DevicesSettings
from .stores import DevicesUnitOfWork

__all__ = [
    "notice_device_bound",
    "purge_signatures",
    "register_notice",
    "register_purge",
    "send_bound_notice",
]

PURGE_TASK: Final = "domainkit.devices.purge_signatures"
NOTICE_TASK: Final = "domainkit.devices.send_bound_notice"
NOTICE_TEMPLATE: Final = "devices/device_bound"


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


async def send_bound_notice(user_id: UUID, device_id: UUID) -> bool:
    """Mail the user that the device ``device_id`` was bound to their account.

    The ``domainkit.devices.send_bound_notice`` task
    (``DEVICES.BOUND_NOTICE``). False, and nothing sent, where the user or
    the device is gone, or the user has no address that takes mail.
    """
    async with DevicesUnitOfWork() as uow:
        user = await uow.users.find(UserId(user_id))
        device = await uow.devices.get(device_id)
    if user is None or not user.is_active or device is None:
        return False
    return await mail_user(user, NOTICE_TEMPLATE, {"device": device.name})


async def notice_device_bound(event: object) -> None:
    """Queue the notice of a ``DeviceBound``, once its transaction committed.

    Nothing with ``DEVICES.BOUND_NOTICE`` off.
    """
    from domainkit.contrib.messaging import enqueue

    if not registry.settings(DevicesSettings).BOUND_NOTICE:
        return
    await enqueue(
        send_bound_notice,
        user_id=event.user_id,  # type: ignore[attr-defined]
        device_id=event.device_id,  # type: ignore[attr-defined]
    )


def register_notice() -> None:
    """Register the notice's task, and send it for every ``DeviceBound``."""
    from domainkit.contrib.messaging import task
    from domainkit.ddd import event_bus

    from .events import DeviceBound

    task(name=NOTICE_TASK)(send_bound_notice)
    event_bus.subscribe(DeviceBound, notice_device_bound)
