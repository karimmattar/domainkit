"""``messages``: inspect the outbox and retry dead letters."""

import uuid
from argparse import ArgumentParser

from domainkit.apps import registry
from domainkit.contrib.messaging.admin import dead_letters, queue_counts, retry_dead
from domainkit.contrib.messaging.settings import MessagingSettings
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Show counts by status, list dead letters, or queue them again."""

    help = "Inspect the messaging outbox."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the listing and retry options."""
        parser.add_argument("--dead", action="store_true", help="list dead letters")
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument(
            "--retry", metavar="ID|all", help="queue a dead message (or all) again"
        )
        parser.add_argument(
            "--database",
            help="the outbox's alias (default: MESSAGING.DATABASE)",
        )

    async def handle(
        self,
        *,
        dead: bool,
        limit: int,
        retry: str | None,
        database: str | None,
        **options: object,
    ) -> None:
        """Print what was asked for."""
        config = registry.settings(MessagingSettings)
        if retry is not None:
            try:
                message_id = None if retry == "all" else uuid.UUID(retry)
            except ValueError:
                raise CommandError(f"{retry!r} is not a message id or 'all'.") from None
            self.write(
                f"Queued {await retry_dead(config, message_id, database)} message(s)."
            )
            return
        if dead:
            for row in await dead_letters(config, limit, database):
                error = (row.last_error or "").splitlines()[0][:120]
                self.write(
                    f"{row.id}  {row.kind} {row.target}  x{row.attempts}  {error}"
                )
            return
        counts = await queue_counts(config, database)
        for status in ("pending", "done", "dead"):
            self.write(f"{status:8} {counts.get(status, 0)}")
