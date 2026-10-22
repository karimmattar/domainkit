"""What a keyed write costs: statements, transactions, and pool checkouts.

A first request claims its key with one statement, marks it committed in
its own transaction, and stores its response with one more; a retry reads
the stored response with two. Claims race safely without a transaction.
"""

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select
from sqlalchemy.engine import Connection, Engine, ExecutionContext
from sqlalchemy.pool import Pool
from starlette.types import Message, Receive, Scope, Send

from domainkit.conf import override_settings
from domainkit.contrib.idempotency import middleware as idempotency
from domainkit.contrib.idempotency.settings import IdempotencySettings
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.db import connections
from domainkit.db.connections import INTERNAL
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.test_ratelimit_and_idempotency import (
    KEYED_POST,
    idempotent_app,
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
    """The sweep of expired keys adds a statement, not a transaction."""
    monkeypatch.setattr(idempotency, "MESSAGING", "not.installed")

    with Costs() as costs:
        idempotent.post("/counter", json={}, headers={"Idempotency-Key": "swept"})

    assert costs.statements == ["DELETE", "INSERT", "UPDATE"]
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
