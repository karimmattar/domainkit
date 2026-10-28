"""What the delivery backends share: mail, text messages, push notifications.

A backend sends a batch of prepared messages and returns how many it sent.
``domainkit.contrib.mail.BaseEmailBackend`` and
``domainkit.contrib.notifications.SmsBackend`` and ``PushBackend`` are
``DeliveryBackend`` classes, each typed on its message; the memory and
console backends of both apps are ``MemoryDelivery`` and
``ConsoleDelivery``. Projects subclass the apps' classes, not these.
"""

import sys
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar

from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.module_loading import import_string

RULE = "-" * 79
"""What the console backends write after each message."""


class DeliveryBackend[M](ABC):
    """Sends messages of one kind somewhere; subclasses say where."""

    deployable: ClassVar[bool] = True
    """False for backends made for development and tests; deployments refuse them."""

    @abstractmethod
    async def send_messages(self, messages: Sequence[M]) -> int:
        """Send ``messages``; return how many were sent."""


class MemoryDelivery[M](DeliveryBackend[M]):
    """Keeps messages in ``outbox``, for tests: nothing leaves the process."""

    deployable = False
    outbox: list[M]
    """Where the messages go, oldest first; each subclass names its list."""

    async def send_messages(self, messages: Sequence[M]) -> int:
        """Append ``messages`` to ``outbox``."""
        self.outbox.extend(messages)
        return len(messages)


class ConsoleDelivery[M](DeliveryBackend[M]):
    """Writes each message to standard output, for development.

    Messages carry codes and links: deployments refuse it.
    """

    deployable = False

    @abstractmethod
    def render(self, message: M) -> str:
        """``message`` as text."""

    async def send_messages(self, messages: Sequence[M]) -> int:
        """Write each message as ``render`` makes it, followed by a rule."""
        for message in messages:
            sys.stdout.write(f"{self.render(message)}\n{RULE}\n")
        sys.stdout.flush()
        return len(messages)


def load_backend(setting: str, path: str, base: type) -> object:
    """A new instance of the class ``path`` names, a subclass of ``base``.

    ``setting`` is how errors name where the path comes from
    (``MAIL.BACKEND``).

    Raises:
        ImproperlyConfigured: ``path`` cannot be imported, or names no
            subclass of ``base``.
    """
    try:
        backend = import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(f"{setting}: {exc}") from exc
    if not (isinstance(backend, type) and issubclass(backend, base)):
        raise ImproperlyConfigured(f"{setting}: {path!r} is not a {base.__name__}.")
    instance: object = backend()
    return instance
