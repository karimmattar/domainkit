"""Email through the outbox (Django's ``django.core.mail``).

Install it with messaging; the worker sends what requests queue::

    INSTALLED_DOMAINS = [
        "domainkit.contrib.messaging",
        "domainkit.contrib.mail",
        ...
    ]
    MAIL = {"DEFAULT_FROM_EMAIL": "Billing <billing@example.com>"}

In a use case, queue the message with the unit of work: it goes out if,
and only if, the change commits::

    async with uow:
        ...
        await send_mail("Welcome", text, [user.email], uow=uow)
        await uow.commit()

    message = render_mail("billing/invoice", {"invoice": invoice}, to=[address])
    await message.send(uow)

Tests use ``MemoryBackend`` and read ``outbox``. See ``docs/mail.md``.
"""

from .backends import (
    BaseEmailBackend,
    ConsoleBackend,
    MailDeliveryFailed,
    MemoryBackend,
    SmtpBackend,
    outbox,
)
from .delivery import deliver, get_backend, send, send_mail
from .message import EmailMessage
from .settings import MailSettings, SmtpSettings
from .templates import TemplateDoesNotExist, TemplateError, render_mail

__all__ = [
    "BaseEmailBackend",
    "ConsoleBackend",
    "EmailMessage",
    "MailDeliveryFailed",
    "MailSettings",
    "MemoryBackend",
    "SmtpBackend",
    "SmtpSettings",
    "TemplateDoesNotExist",
    "TemplateError",
    "deliver",
    "get_backend",
    "outbox",
    "render_mail",
    "send",
    "send_mail",
]
