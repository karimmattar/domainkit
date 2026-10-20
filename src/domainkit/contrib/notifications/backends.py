"""Ports for text messages and push notifications, with development adapters.

``SmsBackend`` and ``PushBackend`` are what a project implements for its
provider (a gateway's HTTP API, a push service); ``NOTIFICATIONS`` names
the classes. The console and memory backends here are for development and
tests: staging and production refuse them.
"""

import sys
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar, Final

from .messages import PushMessage, SmsMessage

sms_outbox: Final[list[SmsMessage]] = []
"""Text messages ``MemorySmsBackend`` sent, oldest first."""
push_outbox: Final[list[PushMessage]] = []
"""Notifications ``MemoryPushBackend`` sent, oldest first."""


class NotificationFailed(Exception):
    """The provider did not take a message; the worker tries again later.

    Adapters raise it with the provider's error code, never a number or a
    token.
    """


class SmsBackend(ABC):
    """Sends text messages through a provider."""

    deployable: ClassVar[bool] = True
    """False for backends made for development and tests; deployments refuse them."""

    @abstractmethod
    async def send_messages(self, messages: Sequence[SmsMessage]) -> int:
        """Send ``messages``; return how many were sent.

        Raises:
            NotificationFailed: The provider refused them, or could not be reached.
        """


class PushBackend(ABC):
    """Sends push notifications through a provider."""

    deployable: ClassVar[bool] = True
    """False for backends made for development and tests; deployments refuse them."""

    @abstractmethod
    async def send_messages(self, messages: Sequence[PushMessage]) -> int:
        """Send ``messages``; return how many were sent.

        Raises:
            NotificationFailed: The provider refused them, or could not be reached.
        """


class MemorySmsBackend(SmsBackend):
    """Keeps text messages in ``sms_outbox``, for tests."""

    deployable = False

    async def send_messages(self, messages: Sequence[SmsMessage]) -> int:
        """Append ``messages`` to ``sms_outbox``."""
        sms_outbox.extend(messages)
        return len(messages)


class MemoryPushBackend(PushBackend):
    """Keeps notifications in ``push_outbox``, for tests."""

    deployable = False

    async def send_messages(self, messages: Sequence[PushMessage]) -> int:
        """Append ``messages`` to ``push_outbox``."""
        push_outbox.extend(messages)
        return len(messages)


class ConsoleSmsBackend(SmsBackend):
    """Writes text messages to standard output, for development."""

    deployable = False

    async def send_messages(self, messages: Sequence[SmsMessage]) -> int:
        """Write each message: its number, then its text."""
        for message in messages:
            sys.stdout.write(f"SMS to {message.to}:\n{message.body}\n{'-' * 79}\n")
        sys.stdout.flush()
        return len(messages)


class ConsolePushBackend(PushBackend):
    """Writes push notifications to standard output, for development."""

    deployable = False

    async def send_messages(self, messages: Sequence[PushMessage]) -> int:
        """Write each notification: its title, text, and data."""
        for message in messages:
            sys.stdout.write(
                f"Push: {message.title}\n{message.body}\n{message.data}\n{'-' * 79}\n"
            )
        sys.stdout.flush()
        return len(messages)
