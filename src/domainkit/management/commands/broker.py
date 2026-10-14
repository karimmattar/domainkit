"""``broker``: check that brokers answer, or declare the worker's queues."""

from argparse import ArgumentParser

from domainkit.broker import BrokerError, brokers
from domainkit.conf import settings
from domainkit.management.base import BaseCommand, CommandError


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
        """Declare every queue the worker's tasks use on ``MESSAGING.BROKER``."""
        from domainkit.apps import registry

        if not registry.is_installed("domainkit.contrib.messaging"):
            raise CommandError("declare needs domainkit.contrib.messaging installed.")
        from domainkit.contrib.messaging import registry as tasks
        from domainkit.contrib.messaging.settings import (
            MessagingSettings,
        )

        alias = registry.settings(MessagingSettings).BROKER
        if alias is None:
            raise CommandError("declare needs MESSAGING.BROKER.")
        queues = sorted(tasks.queues())
        try:
            await brokers[alias].declare(queues)
        except BrokerError as exc:
            raise CommandError(f"{alias}: {exc}") from None
        self.write(f"{alias}: declared {', '.join(queues)}")
