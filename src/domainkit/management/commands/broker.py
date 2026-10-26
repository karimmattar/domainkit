"""``broker``: check that brokers answer, or declare the worker's queues."""

from argparse import ArgumentParser
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from domainkit.broker import BrokerError, brokers
from domainkit.conf import settings
from domainkit.management.base import BaseCommand, CommandError


@runtime_checkable
class QueueDeclarer(Protocol):
    """An installed domain whose worker reads queues from a broker.

    ``domainkit.contrib.messaging`` is one.
    """

    def broker_queues(self) -> tuple[str, Sequence[str]]:
        """The alias of the broker, and the queues to declare on it, in order.

        Raises:
            CommandError: The domain names no broker.
        """
        ...


class Command(BaseCommand):
    """Connect to brokers, or create the exchange and queues the worker uses.

    Declare with an account allowed to configure the virtual host, before
    the workers start, when their own account may only read and write.
    """

    help = "Check that brokers answer, or declare the worker's queues."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the action and, for check, the aliases."""
        parser.add_argument("action", choices=("check", "declare"))
        parser.add_argument(
            "aliases",
            nargs="*",
            metavar="ALIAS",
            help="brokers to check (default: all of them)",
        )

    async def handle(
        self, *, action: str, aliases: list[str], **options: object
    ) -> None:
        """Report each broker, and fail if any could not be reached."""
        if unknown := [alias for alias in aliases if alias not in settings.BROKERS]:
            raise CommandError(f"BROKERS has no {', '.join(map(repr, unknown))}.")
        if action == "declare":
            await self._declare()
            return
        failed = []
        for alias in aliases or list(settings.BROKERS):
            try:
                await brokers[alias].check()
            except BrokerError as exc:
                failed.append(alias)
                self.write(f"{alias}: unavailable ({exc})")
            else:
                self.write(f"{alias}: ok")
        if failed:
            raise CommandError(f"Unavailable: {', '.join(failed)}.")

    async def _declare(self) -> None:
        """Declare every queue the worker's tasks use, on the broker they use.

        The installed domains that run work from a broker say which
        (``QueueDeclarer``: ``domainkit.contrib.messaging``).
        """
        from domainkit.apps import registry

        declarers = [config for config in registry if isinstance(config, QueueDeclarer)]
        if not declarers:
            raise CommandError("declare needs domainkit.contrib.messaging installed.")
        for declarer in declarers:
            alias, queues = declarer.broker_queues()
            try:
                await brokers[alias].declare(queues)
            except BrokerError as exc:
                raise CommandError(f"{alias}: {exc}") from None
            self.write(f"{alias}: declared {', '.join(queues)}")
