"""What operators see of the outbox: listings, states, lag, dead letters, commands."""

import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Final

import pytest
from sqlalchemy import insert

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.messaging import MessagingSettings
from domainkit.contrib.messaging import admin as outbox_admin
from domainkit.contrib.messaging.management.commands.messages import (
    Command as MessagesCommand,
)
from domainkit.contrib.messaging.management.commands.messages import (
    _line as listing_line,
)
from domainkit.contrib.messaging.management.commands.runworker import (
    Command as RunWorker,
)
from domainkit.contrib.messaging.tables import DEAD, DONE, PENDING, messages
from domainkit.db import connections
from domainkit.management import BaseCommand, CommandError
from domainkit_tests.conftest import MakeDatabase

pytestmark = pytest.mark.anyio
NOW: Final = datetime(2026, 10, 30, 12, tzinfo=UTC)
CONFIG: Final = MessagingSettings()


def row(target: str, **values: Any) -> dict[str, Any]:  # noqa: ANN401 - a column's value.
    """An outbox row for ``target``, pending and due an hour ago unless ``values`` say.

    Every row names every column: one insert of several rows takes the
    columns of the first.
    """
    return {
        "id": uuid.uuid4(),
        "kind": "task",
        "target": target,
        "queue": "default",
        "priority": 0,
        "payload": {},
        "headers": {},
        "status": PENDING,
        "attempts": 0,
        "max_attempts": 3,
        "available_at": NOW - timedelta(hours=1),
        "created_at": NOW - timedelta(days=1),
        "locked_until": None,
        "started_at": None,
        "finished_at": None,
    } | values


@pytest.fixture
async def outbox(make_database: MakeDatabase) -> AsyncIterator[None]:
    """An empty outbox in the default database, and one in ``archive``."""
    databases = {
        alias: {"URL": make_database(alias)} for alias in ("default", "archive")
    }
    with override_settings(DATABASES=databases):
        for alias in databases:
            async with connections.engine(alias).begin() as connection:
                await connection.run_sync(messages.create)
        yield
        await connections.close_all()


async def store(*rows: dict[str, Any], database: str = "default") -> None:
    """Insert ``rows`` into the outbox of ``database``."""
    async with connections.engine(database).begin() as connection:
        await connection.execute(insert(messages), list(rows))


async def targets(state: outbox_admin.State, **options: Any) -> list[str]:  # noqa: ANN401
    """The targets ``list_messages`` lists in ``state``, in its order."""
    listed = await outbox_admin.list_messages(CONFIG, state, 10, **options)
    return [each.target for each in listed]


def test_a_message_at_the_edge_of_its_lock_or_its_start_is_still_held_or_due() -> None:
    """Locked until now is still locked; available now is due, as claims see them."""
    running = SimpleNamespace(locked_until=NOW, started_at=NOW, available_at=NOW)
    due = SimpleNamespace(locked_until=None, started_at=None, available_at=NOW)
    later = SimpleNamespace(
        locked_until=None, started_at=None, available_at=NOW + timedelta(seconds=1)
    )

    states = [outbox_admin.state_of(each, NOW) for each in (running, due, later)]

    assert states == ["running", "due", "scheduled"]


async def test_waiting_messages_are_listed_due_first_then_by_priority_and_age(
    outbox: None,
) -> None:
    """Due ones first, the latest due included; locked and dead ones are left out."""
    await store(
        row("scheduled", available_at=NOW + timedelta(minutes=1), priority=9),
        row("due now", available_at=NOW),
        row("older", available_at=NOW - timedelta(hours=2)),
        row("urgent", priority=5),
        row("dead", status=DEAD, finished_at=NOW),
        row("running", locked_until=NOW, started_at=NOW),
    )

    with freeze_time(NOW):
        listed = await targets("pending")

    assert listed == ["urgent", "older", "due now", "scheduled"]


async def test_locked_messages_are_listed_by_the_end_of_their_lock(
    outbox: None,
) -> None:
    """The lock that ends first comes first."""
    await store(
        row("late", locked_until=NOW + timedelta(minutes=5), started_at=NOW),
        row("early", locked_until=NOW + timedelta(minutes=1)),
        row("ended", locked_until=NOW - timedelta(seconds=1), started_at=NOW),
    )

    with freeze_time(NOW):
        listed = await targets("locked")

    assert listed == ["early", "late"]


async def test_dead_letters_are_the_newest_dead_messages(outbox: None) -> None:
    """Newest first, ``limit`` of them, of no other status, from the database named."""
    await store(
        row("old", status=DEAD, finished_at=NOW - timedelta(days=2)),
        row("new", status=DEAD, finished_at=NOW - timedelta(days=1)),
        row("newest", status=DEAD, finished_at=NOW),
        row("done", status=DONE, finished_at=NOW),
        database="archive",
    )
    await store(row("elsewhere", status=DEAD, finished_at=NOW))

    listed = await outbox_admin.dead_letters(CONFIG, 2, "archive")

    assert [each.target for each in listed] == ["newest", "new"]


async def test_listings_keep_to_the_queue_named(outbox: None) -> None:
    """``queue`` leaves the other queues' messages out."""
    await store(row("mail", queue="emails"), row("task"))

    assert await targets("pending", queue="emails") == ["mail"]


async def test_lag_counts_a_message_due_this_very_moment(outbox: None) -> None:
    """A queue whose oldest waiting message became due now has a lag of zero."""
    await store(row("now", queue="emails", available_at=NOW))

    with freeze_time(NOW):
        lag = await outbox_admin.queue_lag(CONFIG)

    assert lag == {"emails": 0.0}


# The commands ---------------------------------------------------------------------


def usage(command: type[BaseCommand], name: str) -> str:
    """The command's ``--help``, on one line."""
    return " ".join(command().create_parser("manage.py", name).format_help().split())


def test_runworker_says_what_each_option_does() -> None:
    """Its ``--help``, as an operator reads it."""
    assert usage(RunWorker, "runworker") == (
        "usage: manage.py runworker [-h] [--once] [--metrics-port METRICS_PORT] "
        "[--metrics-address METRICS_ADDRESS] [--database DATABASE] "
        "[--queue QUEUE] [--no-relay] Run the messaging worker (durable event "
        "handlers, background tasks). options: -h, --help show this help "
        "message and exit --once run the messages that are due now, then exit "
        "(cron, tests) --metrics-port METRICS_PORT serve Prometheus metrics on "
        "this port (needs domainkit[metrics]) --metrics-address METRICS_ADDRESS "
        "address for --metrics-port (default: 127.0.0.1) --database DATABASE a "
        "target (SQL alias or shard group) or an alias whose outboxes to run; "
        "default: every database that hosts domains --queue QUEUE run only this "
        "queue's messages (repeatable); default: every queue --no-relay with a "
        "broker: only consume, never poll the outbox"
    )


@pytest.mark.parametrize(
    "line",
    [
        "--pending list waiting messages, in the order workers take them",
        "--locked list running and published messages",
        "--dead list dead letters",
        "--limit LIMIT",
        "--queue QUEUE list only this queue's messages",
        "--retry ID|all queue a dead message (or all) again",
        "--cancel ID cancel a message no worker is running",
        "--database DATABASE the outbox's alias (default: MESSAGING.DATABASE)",
    ],
)
def test_messages_says_what_each_option_does(line: str) -> None:
    """Each option's line of its ``--help``."""
    assert f" {line} " in f"{usage(MessagesCommand, 'messages')} "


def test_messages_lists_twenty_unless_told() -> None:
    """``--limit`` defaults to 20 messages."""
    parser = MessagesCommand().create_parser("manage.py", "messages")

    assert parser.parse_args(["--dead"]).limit == 20


def test_a_locked_message_is_listed_with_the_end_of_its_lock() -> None:
    """``messages --locked`` shows when the lock ends, not when it was due."""
    locked = SimpleNamespace(
        id="m1",
        kind="task",
        target="tests.t",
        queue="default",
        priority=0,
        attempts=1,
        max_attempts=3,
        available_at=NOW - timedelta(hours=1),
        locked_until=NOW + timedelta(minutes=5),
        started_at=NOW,
    )

    line = listing_line(locked, "locked", NOW)

    assert line.endswith("running 2026-10-30 12:05:00Z")


def test_a_dead_letter_shows_the_first_120_characters_of_its_error() -> None:
    """The first line of the error, cut to fit a terminal."""
    error = "RuntimeError: " + "x" * 200 + "\nTraceback"
    dead = SimpleNamespace(
        id="m1", kind="task", target="tests.t", attempts=3, last_error=error
    )

    line = listing_line(dead, "dead", NOW)

    assert line == f"m1  task tests.t  x3  {error[:120]}"


async def test_metrics_need_the_metrics_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``prometheus_client``, ``--metrics-port`` says which extra to install."""
    monkeypatch.setitem(sys.modules, "prometheus_client", None)

    with pytest.raises(CommandError) as refused:
        await RunWorker().handle(
            once=True,
            metrics_port=9100,
            metrics_address="127.0.0.1",
            database=None,
            queues=None,
            relay=True,
        )

    assert str(refused.value) == "--metrics-port needs domainkit[metrics]."
