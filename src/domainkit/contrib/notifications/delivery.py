"""Queueing text messages and push notifications, and sending them.

As with mail, ``send_sms`` and ``send_push`` store a task in the outbox,
in the unit of work's transaction when one is given: the message goes out
if and only if the change commits. The worker calls the backend, and
tries again (``NOTIFICATIONS.MAX_ATTEMPTS``) while it fails. Delivery is
at least once; every try carries the same ``message_id``.
"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final, cast

from domainkit.apps import registry
from domainkit.contrib.messaging import enqueue
from domainkit.utils.delivery import load_backend

from .backends import PushBackend, SmsBackend
from .messages import PushMessage, SmsMessage
from .settings import NotificationsSettings

if TYPE_CHECKING:
    from domainkit.ddd import UnitOfWork

SMS_TASK: Final = "domainkit.notifications.send_sms"
PUSH_TASK: Final = "domainkit.notifications.send_push"


def notifications_settings() -> NotificationsSettings:
    """The validated ``NOTIFICATIONS`` settings."""
    return registry.settings(NotificationsSettings)


def get_sms_backend() -> SmsBackend:
    """A new instance of ``NOTIFICATIONS.SMS_BACKEND``."""
    path = notifications_settings().SMS_BACKEND
    return cast(SmsBackend, load_backend("NOTIFICATIONS.SMS_BACKEND", path, SmsBackend))


def get_push_backend() -> PushBackend:
    """A new instance of ``NOTIFICATIONS.PUSH_BACKEND``."""
    path = notifications_settings().PUSH_BACKEND
    backend = load_backend("NOTIFICATIONS.PUSH_BACKEND", path, PushBackend)
    return cast(PushBackend, backend)


async def queue_sms(message: SmsMessage, uow: "UnitOfWork | None" = None) -> SmsMessage:
    """Queue ``message`` with ``uow``'s commit, or at once; return it with its id."""
    prepared = message.prepared()
    if uow is None:
        await enqueue(send_queued_sms, message=prepared)
    else:
        uow.defer(send_queued_sms, message=prepared)
    return prepared


async def queue_push(
    message: PushMessage, uow: "UnitOfWork | None" = None
) -> PushMessage:
    """Queue ``message`` with ``uow``'s commit, or at once; return it with its id."""
    prepared = message.prepared()
    if uow is None:
        await enqueue(send_queued_push, message=prepared)
    else:
        uow.defer(send_queued_push, message=prepared)
    return prepared


async def send_sms(
    to: str,
    body: str,
    *,
    sender: str | None = None,
    uow: "UnitOfWork | None" = None,
) -> SmsMessage:
    """Queue a text message to ``to`` (E.164: ``+33612345678``).

    Raises:
        pydantic.ValidationError: The number, the text, or the sender is refused.
    """
    return await queue_sms(SmsMessage(to=to, body=body, sender=sender), uow)


async def send_push(
    token: str,
    title: str,
    body: str,
    *,
    data: Mapping[str, str] | None = None,
    uow: "UnitOfWork | None" = None,
) -> PushMessage:
    """Queue a push notification to the device ``token`` names.

    Raises:
        pydantic.ValidationError: A field is refused, or too long.
    """
    message = PushMessage(token=token, title=title, body=body, data=dict(data or {}))
    return await queue_push(message, uow)


async def deliver_sms(messages: Sequence[SmsMessage]) -> int:
    """Send text messages now, skipping the outbox (from the worker)."""
    return await get_sms_backend().send_messages([m.prepared() for m in messages])


async def deliver_push(messages: Sequence[PushMessage]) -> int:
    """Send push notifications now, skipping the outbox (from the worker)."""
    return await get_push_backend().send_messages([m.prepared() for m in messages])


async def send_queued_sms(message: SmsMessage) -> None:
    """Send one queued text message: the ``send_sms`` task."""
    await get_sms_backend().send_messages([message])


async def send_queued_push(message: PushMessage) -> None:
    """Send one queued push notification: the ``send_push`` task."""
    await get_push_backend().send_messages([message])
