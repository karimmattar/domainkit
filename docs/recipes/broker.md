# A broker of your own

This recipe writes a message broker of your own, and makes the worker
use it. The example keeps its queues in a SQLite file, for a service and
its worker on one machine.

## The problem

The [outbox](../learning.md#words) hands messages to workers through a
broker. domainkit ships brokers for RabbitMQ, Redis Streams, NATS
JetStream, Kafka, and Amazon SQS ([messaging](../contrib/messaging.md)).
You want to carry the messages another way: through a queue service
domainkit does not know, or, as here, a SQLite file on one machine.

You can do this without risk to your data. The outbox stays the record
of every message: a broker that loses a message delays it, and never
drops it.

You need a project with `domainkit[sqlite,testing]` installed. Every step
here is run as written by `domainkit_tests/test_recipes.py`.

## The solution

### Step 1: make a project

```bash
domainkit init jobsAPI jobs
cd jobs
python manage.py makemigrations -m "auth"
python manage.py migrate
```

### Step 2: write the broker

A broker that is asked for messages subclasses
`domainkit.broker.pull.PullBroker`. It does three things:

- `declare(queues)` makes the queues, if the service needs that;
- `publish(queue, body, message_id=...)` keeps a message;
- `puller(queues)` makes a `Puller`, which fetches messages (`fetch`) and
  answers each one: `ack` forgets it, `requeue` hands it out again.

The base class runs the pump that hands the fetched messages to
handlers, at most `prefetch` at once. A message taken and never answered
(its worker died) comes back after `REDELIVER_AFTER` seconds. The broker
is made as `Broker(config)` from its `BROKERS` entry, whose `URL` is the
broker's to read:

```python title="src/config/broker.py (new file)"
"""Work queues in a SQLite file, for one machine."""

import asyncio
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

from domainkit import clock
from domainkit.broker.pull import Fetched, PullBroker, Puller
from domainkit.conf import BrokerConfig

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, "
    "queue TEXT NOT NULL, body BLOB NOT NULL, message_id TEXT NOT NULL, "
    "visible_at REAL NOT NULL)"
)
WAIT = 0.2
"""Seconds a fetch that found nothing waits before answering."""


class SqliteBroker(PullBroker):
    """Messages in a table of the file the URL names: ``sqlitequeue:///path``."""

    def __init__(self, config: BrokerConfig) -> None:
        """Open the file, and make the table if it is new."""
        super().__init__()
        path = urlsplit(config.URL.get_secret_value()).path
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute(SCHEMA)
        self.lock = threading.Lock()
        self.prefix = f"{config.PREFIX}."
        self.redeliver_after = config.REDELIVER_AFTER

    async def run(self, sql: str, *args: object) -> list[tuple[object, ...]]:
        """One statement, on a thread, one at a time."""

        def locked() -> list[tuple[object, ...]]:
            with self.lock:
                return self.db.execute(sql, args).fetchall()

        return await asyncio.to_thread(locked)

    async def declare(self, queues: Iterable[str]) -> None:
        """Nothing to make: a queue is its rows."""

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Keep the message; the file has it once this returns."""
        await self.run(
            "INSERT INTO messages (queue, body, message_id, visible_at) "
            "VALUES (?, ?, ?, 0)",
            self.prefix + queue,
            body,
            message_id,
        )

    async def puller(self, queues: Sequence[str]) -> Puller:
        """A puller of ``queues``."""
        return SqlitePuller(self, [self.prefix + queue for queue in queues])

    async def close(self) -> None:
        """Let deliveries finish, then close the file.

        Under the lock: a fetch cancelled with the pump may still be running
        on its thread, and closing the file under it would crash.
        """
        await super().close()

        def locked() -> None:
            with self.lock:
                self.db.close()

        await asyncio.to_thread(locked)


class SqlitePuller(Puller):
    """Takes messages by hiding them until ``REDELIVER_AFTER`` has passed."""

    def __init__(self, broker: SqliteBroker, queues: list[str]) -> None:
        """Ask ``broker`` for ``queues``."""
        self.broker = broker
        self.queues = queues

    async def fetch(self, limit: int) -> Sequence[Fetched]:
        """Up to ``limit`` visible messages, hidden from other pullers."""
        now = clock.now().timestamp()
        marks = ", ".join("?" * len(self.queues))
        rows = await self.broker.run(
            f"UPDATE messages SET visible_at = ? WHERE id IN (SELECT id "  # noqa: S608
            f"FROM messages WHERE queue IN ({marks}) AND visible_at <= ? "
            "ORDER BY id LIMIT ?) RETURNING id, queue, body, message_id",
            now + self.broker.redeliver_after,
            *self.queues,
            now,
            limit,
        )
        if not rows:
            await asyncio.sleep(WAIT)
        prefix = len(self.broker.prefix)
        return [
            Fetched(str(queue)[prefix:], bytes(body), str(message_id), key)  # type: ignore[arg-type]
            for key, queue, body, message_id in sorted(rows)
        ]

    async def ack(self, message: Fetched) -> None:
        """Forget the message."""
        await self.broker.run("DELETE FROM messages WHERE id = ?", message.handle)

    async def requeue(self, message: Fetched) -> None:
        """Hand the message out again, now."""
        await self.broker.run(
            "UPDATE messages SET visible_at = 0 WHERE id = ?", message.handle
        )
```

### Step 3: name it as the default broker

`BACKEND` makes the broker. The `URL` says where its file is. `PREFIX`,
as for every broker, keeps this service's queues apart from another's:

```python title="src/config/settings/base.py"
BROKERS = {
    "default": {
        "URL": env.str("BROKER_URL", "sqlitequeue:///queue.sqlite3"),
        "PREFIX": "jobs",
        "BACKEND": "config.broker.SqliteBroker",
    }
}
```

The outbox's relay now publishes to the file, and `runworker` consumes
from it.

### Step 4: test the broker

The test publishes two messages, consumes them, requeues the first one
once, and checks that each message reaches the handler and is gone once
acknowledged:

```python title="tests/test_broker.py (new file)"
"""Messages go in the file, out to one handler, and are forgotten once done."""

import asyncio
from pathlib import Path

import pytest
from domainkit.broker import Delivery
from domainkit.conf import BrokerConfig

from config.broker import SqliteBroker

pytestmark = pytest.mark.anyio


def broker_in(path: Path) -> SqliteBroker:
    """A broker on the file ``path``."""
    return SqliteBroker(
        BrokerConfig(
            URL=f"sqlitequeue://{path}",
            PREFIX="jobs",
            BACKEND="config.broker.SqliteBroker",
        )
    )


async def test_each_message_reaches_one_handler_and_is_forgotten(
    tmp_path: Path,
) -> None:
    """Two messages; the first is requeued once, then both are acknowledged."""
    broker = broker_in(tmp_path / "queue.sqlite3")
    seen: list[str] = []
    acknowledged: set[str] = set()
    done = asyncio.Event()

    async def handle(delivery: Delivery) -> None:
        seen.append(delivery.message_id)
        if seen.count("a") == 1 and delivery.message_id == "a":
            await delivery.requeue()
            return
        await delivery.ack()
        acknowledged.add(delivery.message_id)
        if acknowledged == {"a", "b"}:  # Both are gone, not only handed over.
            done.set()

    await broker.publish("emails", b'{"to": "ann"}', message_id="a")
    await broker.publish("emails", b'{"to": "bob"}', message_id="b")
    subscription = await broker.consume(["emails"], 2, handle)
    await asyncio.wait_for(done.wait(), timeout=10)
    await subscription.cancel()

    assert sorted(seen) == ["a", "a", "b"]
    assert await broker.run("SELECT count(*) FROM messages") == [(0,)]
    await broker.close()
```

Run the checks and the tests:

```bash
ruff check --fix .
ruff format --check .
pytest
```

## How it works

| Part | Who calls it | What it must do |
| --- | --- | --- |
| `publish` | the relay, after a commit | Keep the message before it returns. |
| `puller(queues)` | the worker, once | Return a `Puller` of those queues. |
| `Puller.fetch(limit)` | the pump, again and again | Return up to `limit` messages, and hide them from other pullers. Wait a little when there are none. |
| `Puller.ack(message)` | the handler, through `delivery.ack()`, when the message is done | Forget the message. |
| `Puller.requeue(message)` | the handler, through `delivery.requeue()`, to run it again | Hand the message out again. |
| `close()` | the worker, when it stops | Let deliveries finish (`super().close()`), then close what the broker opened. |

Each message reaches one consumer, and is answered once; a message that
nobody answers is delivered again. That is the port's contract, and the
outbox keeps every message until a worker records it as done.

A broker that its server pushes messages to (as RabbitMQ does)
subclasses `domainkit.broker.Broker` instead, and answers `consume`
itself. The port's contract is in [messaging](../contrib/messaging.md).

## See also

- [Backends](../reference/backends.md#brokers): every `BROKERS` key.
- [Extension points](../reference/extension-points.md#backends-from-an-installed-package):
  offer the broker by name from a package.
- [Messaging](../contrib/messaging.md): the outbox, tasks, and the
  worker.
