"""Text messages and push notifications through the outbox.

Install it with messaging, and name a provider adapter for each channel::

    INSTALLED_DOMAINS = [
        "domainkit.contrib.messaging",
        "domainkit.contrib.notifications",
        ...
    ]
    NOTIFICATIONS = {
        "SMS_BACKEND": "accounts.infrastructure.sms.GatewaySmsBackend",
        "PUSH_BACKEND": "accounts.infrastructure.push.FcmPushBackend",
    }

An adapter subclasses ``SmsBackend`` or ``PushBackend``; none ships here.
In a use case, queue with the unit of work, as for mail::

    await send_sms("+33612345678", "Your code is 123456", uow=uow)
    await send_push(device.token, "Shipped", "Your order is on its way", uow=uow)

Tests use the memory backends and read ``sms_outbox`` and ``push_outbox``.
"""

from .backends import (
    ConsolePushBackend,
    ConsoleSmsBackend,
    MemoryPushBackend,
    MemorySmsBackend,
    NotificationFailed,
    PushBackend,
    SmsBackend,
    push_outbox,
    sms_outbox,
)
from .delivery import (
    deliver_push,
    deliver_sms,
    get_push_backend,
    get_sms_backend,
    send_push,
    send_sms,
)
from .messages import PushMessage, SmsMessage
from .settings import NotificationsSettings

__all__ = [
    "ConsolePushBackend",
    "ConsoleSmsBackend",
    "MemoryPushBackend",
    "MemorySmsBackend",
    "NotificationFailed",
    "NotificationsSettings",
    "PushBackend",
    "PushMessage",
    "SmsBackend",
    "SmsMessage",
    "deliver_push",
    "deliver_sms",
    "get_push_backend",
    "get_sms_backend",
    "push_outbox",
    "send_push",
    "send_sms",
    "sms_outbox",
]
