"""``messages``: inspect the outbox, retry dead letters, cancel waiting work."""

import uuid
from argparse import ArgumentParser
from datetime import UTC, datetime
from typing import Any

from domainkit.apps import registry
from domainkit.contrib.messaging.admin import (
    State,
    cancel,
    list_messages,
    queue_counts,
    queue_lag,
    retry_dead,
    state_of,
)
from domainkit.contrib.messaging.settings import MessagingSettings
from domainkit.contrib.messaging.tables import STATUSES
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Show counts and lag, list messages, retry dead ones, or cancel one."""

    help = "Inspect the messaging outbox."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the listing, retry, and cancel options."""
        listing = parser.add_mutually_exclusive_group()
        for state, text in (
            ("pending", "list waiting messages, in the order workers take them"),
            ("locked", "list running and published messages"),
            ("dead", "list dead letters"),
        ):
            listing.add_argument(
                f"--{state}",
                dest="state",
                action="store_const",
                const=state,
                help=text,
            )
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--queue", help="list only this queue's messages")
        parser.add_argument(
            "--retry", metavar="ID|all", help="queue a dead message (or all) again"
        )
        parser.add_argument(
            "--cancel", metavar="ID", help="cancel a message no worker is running"
        )
        parser.add_argument(
            "--database",
            help="the outbox's alias (default: MESSAGING.DATABASE)",
        )

    async def handle(
        self,
        *,
        state: State | None,
        limit: int,
        queue: str | None,
        retry: str | None,
        cancel: str | None,
        database: str | None,
        **options: object,
    ) -> None:
        """Print what was asked for."""
        config = registry.settings(MessagingSettings)
        if retry is not None:
            message_id = None if retry == "all" else _message_id(retry, "or 'all'")
            self.write(
                f"Queued {await retry_dead(config, message_id, database)} message(s)."
            )
            return
        if cancel is not None:
            await self._cancel(config, _message_id(cancel), database)
            return
        if state is not None:
            if limit < 1:
                raise CommandError("--limit must be 1 or more.")
            rows = await list_messages(config, state, limit, database, queue=queue)
            now = datetime.now(UTC)
            for row in rows:
                self.write(_line(row, state, now))
            return
        counts = await queue_counts(config, database)
        for status in STATUSES:
            self.write(f"{status:8} {counts.get(status, 0)}")
        for name, seconds in sorted((await queue_lag(config, database)).items()):
            self.write(f"lag {name}: {seconds:.1f}s")

    async def _cancel(
        self, config: MessagingSettings, message_id: uuid.UUID, database: str | None
    ) -> None:
        """Cancel one message, or explain why it was not."""
        if await cancel(config, message_id, database):
            self.write(f"Cancelled {message_id}.")
            return
        raise CommandError(
            f"{message_id} was not cancelled: it is running, already finished, "
            "or not in this outbox."
        )


def _message_id(text: str, alternative: str = "") -> uuid.UUID:
    """Parse a message id given on the command line."""
    try:
        return uuid.UUID(text)
    except ValueError:
        hint = f" {alternative}" if alternative else ""
        raise CommandError(f"{text!r} is not a message id{hint}.") from None


def _line(row: Any, state: State, now: datetime) -> str:  # noqa: ANN401 - a row.
    """One listed message, without its arguments."""
    if state == "dead":
        error = (row.last_error or "").splitlines()[0][:120]
        return f"{row.id}  {row.kind} {row.target}  x{row.attempts}  {error}"
    when = row.locked_until if state == "locked" else row.available_at
    return (
        f"{row.id}  {row.kind} {row.target}  queue={row.queue} "
        f"priority={row.priority} x{row.attempts}/{row.max_attempts}  "
        f"{state_of(row, now)} {when:%Y-%m-%d %H:%M:%S}Z"
    )
