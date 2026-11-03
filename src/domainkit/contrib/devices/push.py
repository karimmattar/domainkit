"""Push notifications to a user: to every app of theirs that gave a token.

An app registers the token its push service gave it
(``POST /devices/push-tokens``, with its ``platform``: ``fcm``, ``apns``,
or ``huawei``); a token moves to whoever registers it last, and past
``DEVICES.MAX_PUSH_TOKENS`` a user's oldest tokens end. A use case then
notifies the user, through ``domainkit.contrib.notifications``::

    await notify_user(order.customer_id, "Shipped", "On its way", uow=uow)

One notification is queued per token, for the backend of its platform
(``NOTIFICATIONS.PUSH_BACKENDS``). A token its service no longer knows
is forgotten when the worker learns so (``PushTokenExpired``).
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING
from uuid import UUID

from .stores import DevicesUnitOfWork

if TYPE_CHECKING:
    from domainkit.ddd import UnitOfWork

__all__ = ["notify_user"]


async def notify_user(
    user_id: UUID,
    title: str,
    body: str,
    *,
    data: Mapping[str, str] | None = None,
    uow: "UnitOfWork | None" = None,
) -> int:
    """Queue a notification to each of the user's tokens; how many.

    With ``uow``, they go out when it commits (see ``send_push``).

    Raises:
        pydantic.ValidationError: A field is refused, or too long.
    """
    from domainkit.contrib.notifications import PushMessage

    async with DevicesUnitOfWork() as tokens:
        held = await tokens.push_tokens.of_user(user_id)
    for each in held:
        await PushMessage(
            token=each.token,
            title=title,
            body=body,
            data=dict(data or {}),
            platform=each.platform,
        ).send(uow)
    return len(held)
