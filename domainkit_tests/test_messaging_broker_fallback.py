"""While the broker is away, relaying workers run the outbox (user report W3).

With ``MESSAGING.BROKER`` set, workers took their work from the broker
only: a RabbitMQ outage stopped every code, notice, and job, while the
database that holds them all was fine, and the relay leased, unlocked, and
warned about each message every ``POLL_INTERVAL``. Each relay now has a
breaker; while it is open, the worker claims the due messages itself, as a
worker without a broker does (ADR 0142).
"""

import asyncio
import logging
import uuid
from typing import Any

import pytest

from domainkit.apps import registry
from domainkit.broker import BrokerUnavailable, MemoryBroker
from domainkit.conf import override_settings
from domainkit.contrib.messaging import MessagingSettings, Worker, enqueue
from domainkit.contrib.messaging.breaker import Watched
from domainkit.contrib.messaging.dispatch import BrokerWorkers
from domainkit.contrib.messaging.ordering import Promoted
from domainkit.contrib.messaging.tables import DONE
from domainkit_tests.test_messaging import (
    BROKERED,
    broker_tables,
    brokered,
    outbox,
    ran,
    send_email,
    settled,
)

__all__ = ["broker_tables", "brokered"]  # The fixtures the tests take.

pytestmark = pytest.mark.anyio


def quick(**options: object) -> MessagingSettings:
    """The brokered settings, with a breaker that opens at the first failure."""
    return registry.settings(MessagingSettings).model_copy(
        update={"BREAKER_FAILURES": 1, "POLL_INTERVAL": 0.01, **options}
    )


class Outage:
    """The broker's calls fail while it is on; publications are counted."""

    def __init__(self, broker: MemoryBroker, monkeypatch: pytest.MonkeyPatch) -> None:
        """Watch ``broker``'s publications and checks."""
        self.on = True
        self.published: list[str] = []
        publish, check = broker.publish, broker.check

        async def publishing(queue: str, body: bytes, *, message_id: str) -> None:
            if self.on:
                raise BrokerUnavailable("connection refused")
            self.published.append(message_id)
            await publish(queue, body, message_id=message_id)

        async def checking() -> None:
            if self.on:
                raise BrokerUnavailable("connection refused")
            await check()

        monkeypatch.setattr(broker, "publish", publishing)
        monkeypatch.setattr(broker, "check", checking)


async def test_a_relay_runs_the_outbox_while_the_broker_is_away(
    broker_tables: MemoryBroker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The email goes, from the outbox; the log says why, once."""
    outage = Outage(broker_tables, monkeypatch)
    caplog.set_level(logging.WARNING, logger="domainkit.contrib.messaging.breaker")
    await enqueue(send_email, to="ada@example.com")
    config = quick(BREAKER_COOLDOWN=60.0)
    stop = asyncio.Event()
    running = asyncio.create_task(
        BrokerWorkers([Worker(config)], ["default", "emails"], config).run(stop)
    )

    (row,) = await settled(1)
    stop.set()
    await running

    assert row["status"] == DONE
    assert ("email", "ada@example.com") in ran
    assert outage.published == []
    (opened,) = [r.getMessage() for r in caplog.records if "failed" in r.getMessage()]
    assert opened == (
        "Broker 'work' failed 1 time(s) in a row (connection refused); the relay "
        "of 'default' runs its due messages itself, trying again every 60s"
    )


async def test_the_relay_publishes_again_once_the_broker_answers(
    broker_tables: MemoryBroker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After the cooldown, a check that answers closes the breaker."""
    outage = Outage(broker_tables, monkeypatch)
    caplog.set_level(logging.WARNING, logger="domainkit.contrib.messaging.breaker")
    await enqueue(send_email, to="first@example.com")
    config = quick(BREAKER_COOLDOWN=0.05)
    runner = BrokerWorkers([Worker(config)], ["default", "emails"], config)
    stop = asyncio.Event()
    running = asyncio.create_task(runner.run(stop))
    await settled(1)

    outage.on = False
    breaker = runner.breaker("default")
    for _ in range(500):  # The relay's next round after the cooldown.
        if breaker.state == "closed":
            break
        await asyncio.sleep(0.01)
    assert breaker.state == "closed"
    await enqueue(send_email, to="second@example.com")
    rows = await settled(2)
    stop.set()
    await running

    assert [row["status"] for row in rows] == [DONE, DONE]
    assert str(rows[1]["id"]) in outage.published
    assert (
        "Broker 'work' answers again; the relay of 'default' publishes again"
        in caplog.text
    )


async def test_the_next_message_of_a_key_waits_for_the_relay_while_away(
    broker_tables: MemoryBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Promoted with the breaker open, it is left to the outbox, not published."""
    outage = Outage(broker_tables, monkeypatch)
    outage.on = False
    config = quick(BREAKER_COOLDOWN=60.0)
    worker = Worker(config)
    runner = BrokerWorkers([worker], ["default"], config)
    relay: Any = worker._relay_promoted
    breaker = runner.breaker("default")
    breaker.failed(BrokerUnavailable("down"))

    await relay(Promoted(uuid.uuid4(), "default"))  # No such row: nothing to send.
    assert breaker.state == "open"

    breaker.succeeded()
    # Only the relay publishes: on commit, the commit publisher would race it
    # for the message's lease, and the relay find it taken.
    with override_settings(MESSAGING={**BROKERED, "PUBLISH_ON_COMMIT": False}):
        await enqueue(send_email, to="ada@example.com")
    (row,) = await outbox()
    await relay(Promoted(row["id"], "emails"))  # type: ignore[arg-type]
    assert str(row["id"]) in outage.published


async def test_a_failed_probe_opens_the_breaker_again_and_leases_nothing(
    broker_tables: MemoryBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cooled down, a round checks the broker before any row: down, it opens again.

    Nothing is leased to be published; the round runs the outbox instead.
    """
    Outage(broker_tables, monkeypatch)
    tried: list[str] = []
    publishing = broker_tables.publish

    async def counted(queue: str, body: bytes, *, message_id: str) -> None:
        tried.append(message_id)
        await publishing(queue, body, message_id=message_id)

    monkeypatch.setattr(broker_tables, "publish", counted)
    await enqueue(send_email, to="ada@example.com")
    config = quick(BREAKER_COOLDOWN=60.0)
    worker = Worker(config)
    runner = BrokerWorkers([worker], ["default", "emails"], config)
    breaker = runner.breaker("default")
    breaker.failed(BrokerUnavailable("down"))
    assert breaker.opened_at is not None
    breaker.opened_at -= 60  # Cooled down: the next round probes.
    assert breaker.state == "probing"
    running: set[asyncio.Task[None]] = set()

    async with asyncio.TaskGroup() as group:
        done, _ = await runner._round(
            worker, Watched(broker_tables, breaker), group, running, asyncio.Event()
        )

    assert breaker.state == "open"
    assert tried == []
    assert done == 1
    (row,) = await outbox()
    assert row["status"] == DONE
    assert ("email", "ada@example.com") in ran
