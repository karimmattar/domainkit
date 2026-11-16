"""Where mail goes: an SMTP server, the console, or a list for tests.

``MAIL.BACKEND`` names the class. A backend sends prepared messages (with a
sender and an id) and returns how many it sent; it raises
``MailDeliveryFailed`` when the server refuses, and the worker tries again
later, or ``MailRefused`` when the server will refuse the message again,
which the worker does not try again. Write one for a provider's API by
subclassing ``BaseEmailBackend``.
"""

import asyncio
import logging
import smtplib
import ssl
from abc import abstractmethod
from collections.abc import Sequence
from typing import Final

from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.exceptions import DomainkitError, ImproperlyConfigured
from domainkit.utils.delivery import ConsoleDelivery, DeliveryBackend, MemoryDelivery

from .message import EmailMessage
from .settings import MailSettings, SmtpSettings

__all__ = [
    "BaseEmailBackend",
    "ConsoleBackend",
    "MailDeliveryFailed",
    "MailRefused",
    "MemoryBackend",
    "SmtpBackend",
    "outbox",
]

logger = logging.getLogger(__name__)

outbox: Final[list[EmailMessage]] = []
"""Messages ``MemoryBackend`` sent, oldest first (Django's ``mail.outbox``)."""


PERMANENT: Final = range(500, 600)
"""SMTP's permanent replies: the client does not repeat the request (RFC 5321)."""
TOO_MANY_RECIPIENTS: Final = 552
"""A permanent reply to ``RCPT`` that RFC 5321 (4.5.3.1.10) says to take as
temporary: the server's limit of recipients, not the recipient."""


class MailDeliveryFailed(DomainkitError):
    """The server did not take a message; the worker tries again later.

    The message names the error and the server's code, never an address:
    ``code`` keeps the code.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        """Say what failed; keep the server's or the provider's ``code``."""
        super().__init__(message)
        self.code = code


class MailRefused(MailDeliveryFailed):
    """The server will not take this message as it is: it is not tried again.

    A permanent refusal (an SMTP ``5xx`` reply to the sender, to every
    recipient, or to the message; a provider's error for a message it
    cannot take): the worker dead-letters the message at once, and
    reports it, instead of trying it again as it does after an outage.
    """


class BaseEmailBackend(DeliveryBackend[EmailMessage]):
    """Sends messages somewhere; subclasses say where.

    ``deployable`` is False for backends made for development and tests;
    deployments refuse them.
    """

    @abstractmethod
    async def send_messages(self, messages: Sequence[EmailMessage]) -> int:
        """Send prepared ``messages``; return how many were sent.

        Raises:
            MailDeliveryFailed: They could not be sent.
        """


class MemoryBackend(MemoryDelivery[EmailMessage], BaseEmailBackend):
    """Keeps messages in ``outbox``, for tests: nothing leaves the process."""

    outbox = outbox


class ConsoleBackend(ConsoleDelivery[EmailMessage], BaseEmailBackend):
    """Writes each message to standard output, for development.

    Messages carry codes and other secrets: deployments refuse it.
    """

    def render(self, message: EmailMessage) -> str:
        """The message in its MIME form."""
        return message.to_mime().as_string()


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
            raise _failure(exc) from None
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


def _failure(exc: BaseException) -> MailDeliveryFailed:
    """``MailRefused`` for a permanent reply to the message, else a failure.

    Permanent: a ``5xx`` reply to the sender (``MAIL``), to every recipient
    (``RCPT``; a ``552`` there is the server's limit, tried again), or to
    the message (``DATA``). Connecting, greeting, encrypting, and logging
    in are tried again, whatever the reply: an operator may fix them.
    """
    text = _describe(exc)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = sorted({code for code, _ in exc.recipients.values()})
        lasting = bool(codes) and all(
            code in PERMANENT and code != TOO_MANY_RECIPIENTS for code in codes
        )
        code = str(codes[0]) if codes else None
    elif isinstance(exc, smtplib.SMTPSenderRefused | smtplib.SMTPDataError):
        lasting = exc.smtp_code in PERMANENT
        code = str(exc.smtp_code)
    else:
        lasting = False
        smtp_code = getattr(exc, "smtp_code", None)
        code = None if smtp_code is None else str(smtp_code)
    return (MailRefused if lasting else MailDeliveryFailed)(text, code=code)


def _describe(exc: BaseException) -> str:
    """What went wrong, with the server's code: no address, no message text."""
    code = getattr(exc, "smtp_code", None)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = ", ".join(
            str(each) for each in sorted({c for c, _ in exc.recipients.values()})
        )
        return "SMTPRecipientsRefused: the server refused every recipient" + (
            f" ({codes})" if codes else ""
        )
    return f"{type(exc).__name__}" + (f": the server answered {code}" if code else "")
