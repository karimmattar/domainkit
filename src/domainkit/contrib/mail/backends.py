"""Where mail goes: an SMTP server, the console, or a list for tests.

``MAIL.BACKEND`` names the class. A backend sends prepared messages (with a
sender and an id) and returns how many it sent; it raises
``MailDeliveryFailed`` when the server refuses, and the worker tries again
later. Write one for a provider's API by subclassing ``BaseEmailBackend``.
"""

import asyncio
import logging
import smtplib
import ssl
import sys
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar, Final

from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.exceptions import ImproperlyConfigured

from .message import EmailMessage
from .settings import MailSettings, SmtpSettings

logger = logging.getLogger(__name__)

outbox: Final[list[EmailMessage]] = []
"""Messages ``MemoryBackend`` sent, oldest first (Django's ``mail.outbox``)."""


class MailDeliveryFailed(Exception):
    """The server did not take a message; the worker tries again later.

    The message names the error and the server's code, never an address.
    """


class BaseEmailBackend(ABC):
    """Sends messages somewhere; subclasses say where."""

    deployable: ClassVar[bool] = True
    """False for backends made for development and tests; deployments refuse them."""

    @abstractmethod
    async def send_messages(self, messages: Sequence[EmailMessage]) -> int:
        """Send prepared ``messages``; return how many were sent.

        Raises:
            MailDeliveryFailed: They could not be sent.
        """


class MemoryBackend(BaseEmailBackend):
    """Keeps messages in ``outbox``, for tests: nothing leaves the process."""

    deployable = False

    async def send_messages(self, messages: Sequence[EmailMessage]) -> int:
        """Append ``messages`` to ``outbox``."""
        outbox.extend(messages)
        return len(messages)


class ConsoleBackend(BaseEmailBackend):
    """Writes each message to standard output, for development.

    Messages carry reset links and other secrets: deployments refuse it.
    """

    deployable = False

    async def send_messages(self, messages: Sequence[EmailMessage]) -> int:
        """Write ``messages`` in their MIME form, each followed by a rule."""
        for message in messages:
            sys.stdout.write(f"{message.to_mime().as_string()}\n{'-' * 79}\n")
        sys.stdout.flush()
        return len(messages)


class SmtpBackend(BaseEmailBackend):
    """Hands messages to an SMTP server (``MAIL.SMTP``) on a worker thread.

    The connection is encrypted, with the server's certificate checked,
    unless ``SECURITY`` is ``none``, which only ``DEBUG`` allows. Every
    exchange is bounded by ``TIMEOUT``.
    """

    def __init__(self, config: SmtpSettings | None = None) -> None:
        """Use ``config`` (default: ``MAIL.SMTP``).

        Raises:
            ImproperlyConfigured: The connection would be plain text outside
                ``DEBUG``.
        """
        self.config = config or registry.settings(MailSettings).SMTP
        if self.config.SECURITY == "none" and not settings.DEBUG:
            raise ImproperlyConfigured(
                'MAIL.SMTP.SECURITY = "none" sends mail in plain text; use '
                '"starttls" or "tls" (only DEBUG allows it).'
            )

    async def send_messages(self, messages: Sequence[EmailMessage]) -> int:
        """Send ``messages`` over one connection, on a worker thread."""
        if not messages:
            return 0
        return await asyncio.to_thread(self._send, messages)

    def _send(self, messages: Sequence[EmailMessage]) -> int:
        """Connect, send each message, and close the connection.

        Raises:
            MailDeliveryFailed: The server, or the network, failed.
        """
        try:
            connection = self._connect()
            try:
                for message in messages:
                    refused = connection.send_message(
                        message.to_mime(),
                        from_addr=message.from_email,
                        to_addrs=message.recipients(),
                    )
                    if refused:
                        logger.warning(
                            "The SMTP server refused %d of %d recipients of %s",
                            len(refused),
                            len(message.recipients()),
                            message.message_id,
                        )
            finally:
                _close(connection)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailDeliveryFailed(_describe(exc)) from None
        return len(messages)

    def _connect(self) -> smtplib.SMTP:
        """An open, encrypted (unless ``none``), logged-in connection."""
        config = self.config
        context = ssl.create_default_context(
            cafile=None if config.TLS_CA is None else str(config.TLS_CA)
        )
        connection: smtplib.SMTP
        if config.SECURITY == "tls":
            connection = smtplib.SMTP_SSL(
                config.HOST, config.port, timeout=config.TIMEOUT, context=context
            )
        else:
            connection = smtplib.SMTP(config.HOST, config.port, timeout=config.TIMEOUT)
        try:
            if config.SECURITY == "starttls":
                connection.starttls(context=context)
            if config.USER is not None and config.PASSWORD is not None:
                connection.login(config.USER, config.PASSWORD.get_secret_value())
        except BaseException:
            _close(connection)
            raise
        return connection


def _close(connection: smtplib.SMTP) -> None:
    """Say goodbye, or drop the connection if the server cannot hear it."""
    try:
        connection.quit()
    except (smtplib.SMTPException, OSError):
        connection.close()


def _describe(exc: BaseException) -> str:
    """What went wrong, with the server's code: no address, no message text."""
    code = getattr(exc, "smtp_code", None)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "SMTPRecipientsRefused: the server refused every recipient"
    return f"{type(exc).__name__}" + (f": the server answered {code}" if code else "")
