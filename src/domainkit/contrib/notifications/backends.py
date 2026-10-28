"""Ports for text messages and push notifications, with development adapters.

``SmsBackend`` and ``PushBackend`` are what a project implements for its
provider (a gateway's HTTP API, a push service); ``NOTIFICATIONS`` names
the classes. The console and memory backends here are for development and
tests: staging and production refuse them.
"""

from abc import abstractmethod
from collections.abc import Sequence
from typing import Final

from domainkit.exceptions import DomainkitError
from domainkit.utils.delivery import ConsoleDelivery, DeliveryBackend, MemoryDelivery

from .messages import PushMessage, SmsMessage

__all__ = [
    "ConsolePushBackend",
    "ConsoleSmsBackend",
    "MemoryPushBackend",
    "MemorySmsBackend",
    "NotificationFailed",
    "PushBackend",
    "SmsBackend",
    "push_outbox",
    "sms_outbox",
]

sms_outbox: Final[list[SmsMessage]] = []
"""Text messages ``MemorySmsBackend`` sent, oldest first."""
push_outbox: Final[list[PushMessage]] = []
"""Notifications ``MemoryPushBackend`` sent, oldest first."""


class NotificationFailed(DomainkitError):
    """The provider did not take a message; the worker tries again later.

    Adapters raise it with the provider's error code, never a number or a
    token.
    """


class SmsBackend(DeliveryBackend[SmsMessage]):
    """Sends text messages through a provider.

    ``deployable`` is False for backends made for development and tests;
    deployments refuse them.
    """

    @abstractmethod
    async def send_messages(self, messages: Sequence[SmsMessage]) -> int:
        """Send ``messages``; return how many were sent.

        Raises:
            NotificationFailed: The provider refused them, or could not be reached.
        """


class PushBackend(DeliveryBackend[PushMessage]):
    """Sends push notifications through a provider.

    ``deployable`` is False for backends made for development and tests;
    deployments refuse them.
    """

    @abstractmethod
    async def send_messages(self, messages: Sequence[PushMessage]) -> int:
        """Send ``messages``; return how many were sent.

        Raises:
            NotificationFailed: The provider refused them, or could not be reached.
        """


class MemorySmsBackend(MemoryDelivery[SmsMessage], SmsBackend):
    """Keeps text messages in ``sms_outbox``, for tests."""

    outbox = sms_outbox


class MemoryPushBackend(MemoryDelivery[PushMessage], PushBackend):
    """Keeps notifications in ``push_outbox``, for tests."""

    outbox = push_outbox


class ConsoleSmsBackend(ConsoleDelivery[SmsMessage], SmsBackend):
    """Writes text messages to standard output, for development."""

    def render(self, message: SmsMessage) -> str:
        """Its number, then its text."""
        return f"SMS to {message.to}:\n{message.body}"


class ConsolePushBackend(ConsoleDelivery[PushMessage], PushBackend):
    """Writes push notifications to standard output, for development."""

    def render(self, message: PushMessage) -> str:
        """Its title, text, and data."""
        return f"Push: {message.title}\n{message.body}\n{message.data}"
