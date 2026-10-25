"""What a keyed write costs: statements, transactions, and pool checkouts.

A first request claims its key with one statement, marks it committed in
its own transaction, and stores its response with one more; a retry reads
the stored response with two. Claims race safely without a transaction.
"""

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select
from sqlalchemy.engine import Connection, Engine, ExecutionContext
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.pool import Pool
from sqlalchemy.sql import Executable
from starlette.types import Message, Receive, Scope, Send

from domainkit import deadlines, setup
from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.idempotency import middleware as idempotency
from domainkit.contrib.idempotency.operations import Operation
from domainkit.contrib.idempotency.settings import IdempotencySettings
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.db import connections
from domainkit.db.connections import INTERNAL
from domainkit_tests import sample_routes
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.test_ratelimit_and_idempotency import (
    KEYED_POST,
    discard,
    idempotent_app,
    payments_on,
    receive_empty_body,
    stored_keys,
)

ON_POSTGRES = bool(os.environ.get(POSTGRES_VARIABLE))
postgres_only = pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}")
RACERS = 5
CREDENTIALS = IdempotencySettings().CREDENTIAL_HEADERS


class Costs:
    """The statements, transactions, and checkouts of every pool while entered.

    Statements are counted by their first word, without the framework's own
    (``INTERNAL``). A transaction is a ``BEGIN`` on a connection that is not
    in ``AUTOCOMMIT``.
    """

    def __init__(self) -> None:
        """Start at nothing."""
        self.statements: list[str] = []
        self.transactions = 0
        self.checkouts = 0

    def __enter__(self) -> Self:
        """Start counting."""
        event.listen(Engine, "before_cursor_execute", self._statement)
        event.listen(Engine, "begin", self._begin)
        event.listen(Pool, "checkout", self._checkout)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop counting."""
        event.remove(Engine, "before_cursor_execute", self._statement)
        event.remove(Engine, "begin", self._begin)
        event.remove(Pool, "checkout", self._checkout)

    def _statement(
        self,
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: Any,  # noqa: ANN401 - never read.
        context: ExecutionContext | None,
        executemany: bool,
    ) -> None:
        if context is None or not context.execution_options.get(INTERNAL):
            self.statements.append(statement.split(None, 1)[0].upper())

    def _begin(self, connection: Connection) -> None:
        options = connection.get_execution_options()
        if options.get("isolation_level") != "AUTOCOMMIT":
            self.transactions += 1

    def _checkout(self, *args: object) -> None:
        self.checkouts += 1


def test_a_keyed_write_claims_and_stores_with_one_statement_each(
    idempotent: TestClient,
) -> None:
    """Three checkouts and one transaction: the request's, which marks the key.

    The claim used to take two transactions (a sweep, then the insert), and
    the store a third.
    """
    with Costs() as first:
        paid = idempotent.post("/pay", json={}, headers={"Idempotency-Key": "costs"})

    assert paid.status_code == 201
    # The claim, the payment, the mark in the payment's transaction, the store.
    assert first.statements == ["INSERT", "INSERT", "UPDATE", "UPDATE"]
    assert (first.transactions, first.checkouts) == (1, 3)

    with Costs() as retry:
        again = idempotent.post("/pay", json={}, headers={"Idempotency-Key": "costs"})

    assert again.headers["Idempotent-Replayed"] == "true"
    assert retry.statements == ["INSERT", "SELECT"]  # The claim, then the row.
    assert (retry.transactions, retry.checkouts) == (0, 1)


def test_a_claim_without_a_worker_sweeps_on_the_same_connection(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep of expired keys adds a statement, not a transaction.

    It comes after the claim: through PgBouncer they share a transaction,
    whose sweep must hold no locks while its upsert waits.
    """
    monkeypatch.setattr(idempotency, "MESSAGING", "not.installed")

    with Costs() as costs:
        idempotent.post("/counter", json={}, headers={"Idempotency-Key": "swept"})

    assert costs.statements == ["INSERT", "DELETE", "UPDATE"]
    assert (costs.transactions, costs.checkouts) == (0, 2)


@postgres_only
def test_through_pgbouncer_claims_and_stores_are_transactions(
    database_url: str,
) -> None:
    """Their timeouts need a transaction to be set in: each is one."""
    bounced = {"default": {"URL": database_url, "PGBOUNCER": True}}
    with idempotent_app({"default": database_url}) as client:
        with override_settings(DATABASES=bounced), Costs() as costs:
            paid = client.post("/pay", json={}, headers={"Idempotency-Key": "pgb"})
            again = client.post("/pay", json={}, headers={"Idempotency-Key": "pgb"})
        stored = stored_keys()

    assert (paid.status_code, again.headers["Idempotent-Replayed"]) == (201, "true")
    assert costs.transactions == 4  # Claim, payment, store; the retry's claim.
    assert [row["status_code"] for row in stored] == [201]


class Pausing:
    """A claim's connection that waits after its first statement until let go."""

    def __init__(
        self, connection: AsyncConnection, ran: asyncio.Event, go: asyncio.Event
    ) -> None:
        """Wrap ``connection``: set ``ran`` after its first statement, then wait."""
        self.connection, self.ran, self.go = connection, ran, go
        self.dialect = connection.dialect

    async def _after(self, result: Any) -> Any:  # noqa: ANN401 - SQLAlchemy's.
        if not self.ran.is_set():
            self.ran.set()
            await self.go.wait()
        return result

    async def execute(
        self, statement: Executable, parameters: dict[str, Any] | None = None
    ) -> Any:  # noqa: ANN401
        """Run ``statement``; the first one waits afterwards."""
        return await self._after(await self.connection.execute(statement, parameters))

    async def scalar(
        self, statement: Executable, parameters: dict[str, Any] | None = None
    ) -> Any:  # noqa: ANN401
        """Run ``statement`` for one value; the first one waits afterwards."""
        return await self._after(await self.connection.scalar(statement, parameters))


@postgres_only
def test_claims_through_pgbouncer_never_wait_holding_their_sweep(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two claims whose keys each sweep would delete both get their keys.

    Through PgBouncer a claim and its sweep share a transaction. Each
    claim here pauses after its first statement until both have run one.
    Had each swept first, holding the other's expired key, both upserts
    would wait for each other: a deadlock (SQLSTATE 40P01), refused as 503.
    """
    monkeypatch.setattr(idempotency, "MESSAGING", "not.installed")
    expired = datetime.now(UTC) - timedelta(days=3)
    real = connections.autocommit
    pauses = {name: (asyncio.Event(), asyncio.Event()) for name in ("A", "B")}

    @asynccontextmanager
    async def pausing(alias: str = "default") -> AsyncIterator[Any]:
        task = asyncio.current_task()
        async with real(alias) as connection:
            yield Pausing(connection, *pauses[task.get_name() if task else ""])

    async def scenario() -> list[object]:
        async with connections.engine().begin() as connection:
            await connection.run_sync(idempotency_keys.create)
            await connection.execute(
                insert(idempotency_keys),
                [
                    {
                        "key_hash": f"{n:064d}",
                        "fingerprint": "f" * 64,
                        "created_at": expired,
                        "expires_at": expired + timedelta(seconds=n),  # 0 oldest.
                        "token": "old",
                        "locked_until": expired,
                    }
                    for n in range(2 * idempotency.PURGE_BATCH)
                ],
            )
        middleware = idempotency.IdempotencyMiddleware(app=None)  # type: ignore[arg-type]
        config = IdempotencySettings()
        monkeypatch.setattr(connections, "autocommit", pausing)

        async def claim(key: int) -> object:
            operation = Operation(f"{key:064d}", secrets.token_hex(16))
            try:
                return await middleware._claim(config, operation, "g" * 64)
            except DBAPIError as exc:
                return exc

        # A's key is in the second sweep's batch, B's in the first's.
        a = asyncio.create_task(claim(150), name="A")
        await pauses["A"][0].wait()
        b = asyncio.create_task(claim(50), name="B")
        await pauses["B"][0].wait()
        for _, go in pauses.values():
            go.set()
        claimed = list(await asyncio.gather(a, b))
        monkeypatch.setattr(connections, "autocommit", real)
        await connections.close_all()
        return claimed

    bounced = {"default": {"URL": database_url, "PGBOUNCER": True}}
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.idempotency",
            "domainkit_tests.sample_domains.alpha",
        ],
        DATABASES=bounced,
    ):
        registry.clear()
        setup()
        claimed = asyncio.run(scenario())
        registry.clear()

    assert claimed == [None, None]  # Both claimed their keys.


@pytest.mark.usefixtures("idempotent")
def test_of_requests_racing_with_one_key_exactly_one_runs() -> None:
    """The others are told it is in progress; once it is done, retries replay it."""
    runs: list[Scope] = []

    async def scenario() -> list[list[Message]]:
        release = asyncio.Event()

        async def handler(scope: Scope, receive: Receive, send: Send) -> None:
            runs.append(scope)
            await release.wait()
            await send({"type": "http.response.start", "status": 201, "headers": []})
            await send({"type": "http.response.body", "body": b"paid"})

        async def request() -> list[Message]:
            sent: list[Message] = []

            async def send(message: Message) -> None:
                sent.append(message)

            await idempotency.IdempotencyMiddleware(handler)(
                KEYED_POST, receive_empty_body, send
            )
            return sent

        async with asyncio.timeout(30):  # Two winners would wait for ever.
            racing = [asyncio.create_task(request()) for _ in range(RACERS)]
            pending = set(racing)
            while len(pending) > 1:  # The losers answer at once.
                _, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
            release.set()
            answers = [await task for task in racing]
            answers.append(await request())  # A retry, once it is done.
        await connections.close_all()
        return answers

    *raced, retried = asyncio.run(scenario())

    assert len(runs) == 1
    statuses = sorted(answer[0]["status"] for answer in raced)
    assert statuses == [201] + [409] * (RACERS - 1)
    assert retried[0]["status"] == 201
    assert (b"idempotent-replayed", b"true") in retried[0]["headers"]
    assert retried[1]["body"] == b"paid"


@pytest.fixture
def idempotent(database_url: str) -> Iterator[TestClient]:
    """An app with the idempotency app and middleware on an empty database."""
    with idempotent_app({"default": database_url}) as client:
        yield client


@pytest.mark.usefixtures("idempotent")
def test_an_expired_key_is_claimed_with_nothing_of_its_old_row() -> None:
    """Replaced in place, it is in progress, unpinned, and released if it fails.

    The old row had finished, committed, been pinned, and held another
    request's fingerprint: none of it may survive the claim.
    """
    now = datetime.now(UTC)
    old = {
        "key_hash": idempotency._hash(
            idempotency._caller(KEYED_POST, CREDENTIALS), "raw"
        ),
        "fingerprint": "another request",
        "status_code": 201,
        "headers": {"content-type": "text/plain"},
        "body": b"old",
        "created_at": now - timedelta(days=2),
        "expires_at": now - timedelta(seconds=1),
        "committed_at": now - timedelta(days=2),
        "token": "first",
        "locked_until": now - timedelta(seconds=1),
        "pinned": True,
    }
    seen: list[dict[str, Any]] = []

    async def failing(scope: Scope, receive: Receive, send: Send) -> None:
        async with connections.engine().connect() as connection:
            row = (await connection.execute(select(idempotency_keys))).one()
        seen.append(dict(row._mapping))
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b"later"})

    async def scenario() -> list[Message]:
        async with connections.engine().begin() as connection:
            await connection.execute(insert(idempotency_keys).values(**old))
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        await idempotency.IdempotencyMiddleware(failing)(
            KEYED_POST, receive_empty_body, send
        )
        await connections.close_all()
        return sent

    answered = asyncio.run(scenario())

    assert answered[0]["status"] == 503  # It ran: no replay, no mismatch.
    [claimed] = seen
    assert claimed["fingerprint"] == idempotency._hash("POST", "/raw", b"", b"")
    assert (claimed["token"], claimed["pinned"]) != ("first", True)
    assert claimed["pinned"] is False
    for gone in ("status_code", "headers", "body", "committed_at"):
        assert claimed[gone] is None, gone
    assert claimed["created_at"] > old["created_at"]
    assert claimed["expires_at"] > now
    assert claimed["locked_until"] > now
    assert stored_keys() == []  # Released: the failure committed nothing.


def test_pins_and_late_marks_keep_to_a_deadline_no_timer_enforces(
    make_database: Callable[[str], str],
) -> None:
    """A task that shares the request's deadline, but not its timer, commits elsewhere.

    The pin before that commit and the mark after it commit as they run,
    with no transaction to lower ``statement_timeout`` in: each runs under
    a timer of its own, cut off at the deadline.
    """
    databases = {"default": make_database("keys"), "other": make_database("other")}
    seen: list[tuple[str, bool]] = []

    def record(*args: Any) -> None:  # noqa: ANN401 - before_cursor_execute's.
        statement: str = args[2]
        if statement.startswith("UPDATE domainkit_idempotency_keys"):
            which = "pin" if "pinned=" in statement else "mark"
            if "committed_at=" in statement or which == "pin":
                seen.append((which, deadlines.enforced()))

    async def pay(scope: Scope, receive: Receive, send: Send) -> None:
        async def work() -> None:
            async with sample_routes.PaymentsUnitOfWork(database="other") as uow:
                await uow.session.execute(insert(sample_routes.payments))
                await uow.commit()

        async with deadlines.timeout(60.0):
            await asyncio.create_task(work())  # Shares the deadline only.
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"paid"})

    async def scenario() -> None:
        await idempotency.IdempotencyMiddleware(pay)(
            KEYED_POST, receive_empty_body, discard
        )
        await connections.close_all()

    with idempotent_app(databases):
        event.listen(Engine, "before_cursor_execute", record)
        try:
            asyncio.run(scenario())
        finally:
            event.remove(Engine, "before_cursor_execute", record)
        assert payments_on("other") == 1

    assert seen == [("pin", True), ("mark", True)]


def test_a_deadline_that_passed_changes_nothing_for_the_pin_and_mark(
    make_database: Callable[[str], str],
) -> None:
    """As for a transaction's timeouts: the commit and its mark still happen.

    The clock moves past the deadline, which no timer has cut off yet.
    """
    databases = {"default": make_database("keys"), "other": make_database("other")}

    async def pay(scope: Scope, receive: Receive, send: Send) -> None:
        async def work() -> None:
            async with sample_routes.PaymentsUnitOfWork(database="other") as uow:
                await uow.session.execute(insert(sample_routes.payments))
                await uow.commit()

        with freeze_time() as clock:
            async with deadlines.timeout(60.0):
                clock.tick(61)
                assert deadlines.remaining() == 0
                await asyncio.create_task(work())  # Shares the deadline only.
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"paid"})

    async def scenario() -> None:
        await idempotency.IdempotencyMiddleware(pay)(
            KEYED_POST, receive_empty_body, discard
        )
        await connections.close_all()

    with idempotent_app(databases):
        asyncio.run(scenario())
        assert payments_on("other") == 1
        [row] = stored_keys()

    assert (row["status_code"], row["pinned"]) == (201, True)
    assert row["committed_at"] is not None
