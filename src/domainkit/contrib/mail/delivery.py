"""Queueing mail with the transaction, and sending it from the worker.

``send`` stores a ``domainkit.mail.send`` task in the outbox: in the unit
of work's transaction when one is given, so the message goes out if and
only if the change commits, and in its own otherwise. The worker then
calls the backend, and tries again (``MAIL.MAX_ATTEMPTS``) while it fails.
Delivery is at least once: every try sends the same ``Message-ID``, so
mail clients can drop a copy that comes twice.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from domainkit.apps import registry
from domainkit.contrib.messaging import enqueue
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.module_loading import import_string

from .backends import BaseEmailBackend
from .message import EmailMessage
from .settings import MailSettings

if TYPE_CHECKING:
    from domainkit.ddd import UnitOfWork

SEND_TASK: Final = "domainkit.mail.send"
"""The name of the task that sends one queued message."""


def mail_settings() -> MailSettings:
    """The validated ``MAIL`` settings."""
    return registry.settings(MailSettings)


def get_backend() -> BaseEmailBackend:
    """A new instance of ``MAIL.BACKEND``.

    Raises:
        ImproperlyConfigured: The path names no ``BaseEmailBackend``.
    """
    path = mail_settings().BACKEND
    try:
        backend = import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(f"MAIL.BACKEND: {exc}") from exc
    if not (isinstance(backend, type) and issubclass(backend, BaseEmailBackend)):
        raise ImproperlyConfigured(f"MAIL.BACKEND: {path!r} is not a BaseEmailBackend.")
    instance: BaseEmailBackend = backend()
    return instance


def prepare(message: EmailMessage) -> EmailMessage:
    """``message`` with its sender (``MAIL.DEFAULT_FROM_EMAIL``) and an id."""
    return message.prepared(mail_settings().DEFAULT_FROM_EMAIL)


async def deliver(messages: Sequence[EmailMessage]) -> int:
    """Send ``messages`` now, through the backend; return how many were sent.

    For background work that is already running in the worker, such as a
    task that makes a reset link: it skips the outbox, so nothing the
    messages hold is stored there. Requests should ``send`` instead.

    Raises:
        MailDeliveryFailed: The backend could not send them.
    """
    return await get_backend().send_messages([prepare(m) for m in messages])


async def send(message: EmailMessage, uow: "UnitOfWork | None" = None) -> EmailMessage:
    """Queue ``message``; return it with its sender and id.

    With ``uow``, the message is stored with its commit, and dropped if it
    rolls back. Without, it is stored at once, in a transaction of its own.
    """
    prepared = prepare(message)
    if uow is None:
        await enqueue(send_queued, message=prepared)
    else:
        uow.defer(send_queued, message=prepared)
    return prepared


async def send_mail(
    subject: str,
    message: str,
    recipient_list: Sequence[str],
    *,
    from_email: str | None = None,
    html_message: str | None = None,
    uow: "UnitOfWork | None" = None,
) -> EmailMessage:
    """Queue one message to ``recipient_list`` (Django's ``send_mail``).

    ``message`` is the plain-text body, ``html_message`` an HTML
    alternative. See ``send`` for ``uow``.

    Raises:
        pydantic.ValidationError: An address, the subject, or a size is refused.
    """
    email = EmailMessage(
        subject=subject,
        body=message,
        to=tuple(recipient_list),
        from_email=from_email,
        html=html_message,
    )
    return await send(email, uow)


async def send_queued(message: EmailMessage) -> None:
    """Send one queued message: the ``domainkit.mail.send`` task."""
    await get_backend().send_messages([message])
