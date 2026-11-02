"""``messages``: inspect the outbox, retry dead letters, cancel waiting work.

``manage.py messages status | list STATE | retry ID|all | cancel ID | release``;
``release`` repairs ordering keys left with held messages and none pending,
as each worker's cleanup does. ``retry``, ``cancel``, and ``release`` change
the outbox, and run as the operator who names themselves.
"""

import uuid
from datetime import datetime
from typing import Any

from domainkit import clock
from domainkit.apps import registry
from domainkit.contrib.messaging.admin import (
    State,
    blanked_dead_letters,
    list_messages,
    queue_counts,
    queue_lag,
    release_stalled,
    retry_dead,
    state_of,
)
from domainkit.contrib.messaging.admin import (
    cancel as cancel_message,
)
from domainkit.contrib.messaging.settings import MessagingSettings
from domainkit.contrib.messaging.tables import STATUSES
from domainkit.management import ActionCommand, CommandError, action, option


class Command(ActionCommand):
    """Show counts and lag, list messages, retry dead ones, cancel or release."""

    help = "Inspect the messaging outbox, and act on it."
    common_options = (
        option("--database", help="the outbox's alias (default: MESSAGING.DATABASE)"),
    )

    @action(help="count messages by status, and each queue's lag")
    async def status(self, *, database: str | None) -> None:
        """Print a count per status, then each queue's lag."""
        config = registry.settings(MessagingSettings)
        counts = await queue_counts(config, database)
        for status in STATUSES:
            self.write(f"{status:8} {counts.get(status, 0)}")
        for name, seconds in sorted((await queue_lag(config, database)).items()):
            self.write(f"lag {name}: {seconds:.1f}s")

    @action(
        name="list",
        help="list messages in a state: pending (in the order workers take "
        "them), held (waiting for an earlier one of their key), locked "
        "(running and published), or dead",
        options=[
            option("state", choices=("pending", "held", "locked", "dead")),
            option(
                "--limit", type=int, default=20, help="at most this many (default: 20)"
            ),
            option("--queue", help="only this queue's messages"),
        ],
    )
    async def listed(
        self, *, state: State, limit: int, queue: str | None, database: str | None
    ) -> None:
        """Print one line per message, without its arguments.

        Raises:
            CommandError: ``--limit`` below 1.
        """
        if limit < 1:
            raise CommandError("--limit must be 1 or more.")
        config = registry.settings(MessagingSettings)
        rows = await list_messages(config, state, limit, database, queue=queue)
        now = clock.now()
        for row in rows:
            self.write(_line(row, state, now))

    @action(
        help="queue a dead message, or all of them, again",
        options=[option("message", metavar="ID|all")],
        operator=True,
    )
    async def retry(self, *, message: str, database: str | None) -> None:
        """Queue dead messages again; say which ones cannot be, and why."""
        config = registry.settings(MessagingSettings)
        message_id = None if message == "all" else _message_id(message, "or 'all'")
        await self._retry(config, message_id, database)

    @action(
        help="cancel a message no worker is running",
        options=[option("message", metavar="ID")],
        operator=True,
    )
    async def cancel(self, *, message: str, database: str | None) -> None:
        """Cancel one message, or explain why it was not."""
        config = registry.settings(MessagingSettings)
        await self._cancel(config, _message_id(message), database)

    @action(
        help="run the next message of each ordering key left with held messages "
        "and none pending",
        operator=True,
    )
    async def release(self, *, database: str | None) -> None:
        """Release stalled ordering keys, as each worker's cleanup does."""
        config = registry.settings(MessagingSettings)
        async with self.recorded(
            "messages.release", database=database or config.DATABASE
        ) as record:
            record["released"] = released = await release_stalled(config, database)
        self.write(f"Released {released} stalled key(s).")

    async def _retry(
        self,
        config: MessagingSettings,
        message_id: uuid.UUID | None,
        database: str | None,
    ) -> None:
        """Queue dead messages again; say which ones cannot be, and why.

        The retry is recorded in the audit trail, with how many went.
        """
        async with self.recorded(
            "messages.retry",
            message="all" if message_id is None else str(message_id),
            database=database or config.DATABASE,
        ) as action:
            queued = await retry_dead(config, message_id, database)
            left = await blanked_dead_letters(config, message_id, database)
            action.update(queued=queued, blanked=left)
            if message_id is not None and left:
                raise CommandError(
                    f"{message_id} was not queued: its task is sensitive, so its "
                    "arguments were blanked when it died. Make and send it again "
                    "from where it came from."
                )
        self.write(f"Queued {queued} message(s).")
        if left:
            self.write(
                f"Left {left} dead message(s) of sensitive tasks: their "
                "arguments were blanked when they died."
            )

    async def _cancel(
        self, config: MessagingSettings, message_id: uuid.UUID, database: str | None
    ) -> None:
        """Cancel one message, or explain why it was not; record the attempt."""
        async with self.recorded(
            "messages.cancel",
            message=str(message_id),
            database=database or config.DATABASE,
        ) as action:
            action["cancelled"] = cancelled = await cancel_message(
                config, message_id, database
            )
        if cancelled:
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
        error = next(iter((row.last_error or "").splitlines()), "")[:120]
        return f"{row.id}  {row.kind} {row.target}  x{row.attempts}  {error}"
    if state == "held":
        return (
            f"{row.id}  {row.kind} {row.target}  queue={row.queue} "
            f"key={row.ordering_key}  x{row.attempts}/{row.max_attempts}"
        )
    when = row.locked_until if state == "locked" else row.available_at
    return (
        f"{row.id}  {row.kind} {row.target}  queue={row.queue} "
        f"priority={row.priority} x{row.attempts}/{row.max_attempts}  "
        f"{state_of(row, now)} {when:%Y-%m-%d %H:%M:%S}Z"
    )
