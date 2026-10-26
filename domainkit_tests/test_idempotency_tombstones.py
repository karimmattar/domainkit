"""A committed key is never run again once its TTL passes (FIN-84).

Its response is dropped once the TTL passes, but the key is kept, without
it, for ``TOMBSTONE_SECONDS`` more: a retry gets 409 rather than running
the request a second time.

These tests set ``ANONYMOUS`` themselves, so they hold whatever its
default.
"""

import asyncio
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import ValidationError
from sqlalchemy import insert, select, update

from domainkit import setup
from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.idempotency import middleware as idempotency
from domainkit.contrib.idempotency import operations
from domainkit.contrib.idempotency.operations import Operation
from domainkit.contrib.idempotency.settings import IdempotencySettings
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.contrib.idempotency.tasks import purge_expired
from domainkit.db import connections
from domainkit.ddd.personal import SEPARATOR
from domainkit.http import get_asgi_application
from domainkit_tests import sample_routes

MIDDLEWARE: Final = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
    "domainkit.contrib.idempotency.middleware.IdempotencyMiddleware",
]
TOMBSTONE: Final = timedelta(days=7)
"""``TOMBSTONE_SECONDS`` by default."""
BURIED: Final = (
    "The request with this Idempotency-Key already ran, and its response is no "
    "longer kept; look up the result instead of retrying."
)
KEY: Final = {"Idempotency-Key": "pay-ann"}


@contextmanager
def application(database_url: str, **idempotency: object) -> Iterator[TestClient]:
    """Idempotency (purged hourly by messaging) and the payment routes."""
    sample_routes.created.clear()
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.messaging",
            "domainkit.contrib.idempotency",
            "domainkit_tests.sample_domains.alpha",
        ],
        DATABASES={"default": {"URL": database_url}},
        IDEMPOTENCY={
            "ANONYMOUS": "per-address",
            "MAX_RESPONSE_BYTES": 1024,
            **idempotency,
        },
        MIDDLEWARE=MIDDLEWARE,
    ):
        setup()

        async def create() -> None:
            async with connections.engine().begin() as connection:
                await connection.run_sync(idempotency_keys.create)
                await connection.run_sync(sample_routes.payments.create)
            await connections.close_all()

        asyncio.run(create())
        with TestClient(
            get_asgi_application(), raise_server_exceptions=False
        ) as test_client:
            yield test_client
    registry.clear()


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    """The application with the default settings."""
    with application(database_url) as test_client:
        yield test_client


def run[T](work: Coroutine[Any, Any, T]) -> T:
    """Run ``work`` and close the connections it opened."""

    async def closing() -> T:
        try:
            return await work
        finally:
            await connections.close_all()

    return asyncio.run(closing())


def rows() -> list[dict[str, Any]]:
    """The idempotency table's rows."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            found = (await connection.execute(select(idempotency_keys))).all()
        return [dict(row._mapping) for row in found]

    return run(read())


def row_of(key: str) -> dict[str, Any]:
    """The row of ``key``, sent by the test client."""
    [found] = [row for row in rows() if row["key_hash"] == keyed(key)]
    return found


def payments() -> int:
    """How many payments were made."""

    async def count() -> int:
        async with connections.engine().connect() as connection:
            found = (await connection.execute(select(sample_routes.payments))).all()
        return len(found)

    return run(count())


def purge() -> None:
    """Run the hourly purge once."""

    async def once() -> None:
        await purge_expired()

    run(once())


def keyed(key: str) -> str:
    """The hash of ``key`` sent by the test client, without credentials."""
    scope = {"type": "http", "headers": [], "client": ("testclient", 50000)}
    caller = idempotency._caller(scope, IdempotencySettings().CREDENTIAL_HEADERS)
    return idempotency._hash(caller, key)


def plant(key: str, **values: object) -> None:
    """Store ``key`` for a POST to ``/pay`` with an empty body, expired."""
    now = datetime.now(UTC)
    row = {
        "key_hash": keyed(key),
        "fingerprint": idempotency._hash("POST", "/pay", b"", b""),
        "created_at": now - timedelta(days=1),
        "expires_at": now - timedelta(seconds=1),
        "token": "first",
        "locked_until": now - timedelta(seconds=1),
        **values,
    }

    async def store() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(insert(idempotency_keys).values(**row))

    run(store())


def expiry(key: str = KEY["Idempotency-Key"]) -> datetime:
    """When ``key``'s response expires."""
    expires_at = row_of(key)["expires_at"]
    assert isinstance(expires_at, datetime)
    return expires_at


def assert_buried(response: Response) -> None:
    """``response`` refuses a key that ran and whose response is gone."""
    assert (response.status_code, response.json()["code"]) == (
        409,
        "idempotency_response_unavailable",
    )
    assert response.json()["detail"] == BURIED
    assert "Retry-After" not in response.headers


@pytest.mark.parametrize("purged", [True, False], ids=["purged", "expired"])
def test_a_committed_key_is_not_run_again_once_its_ttl_passes(
    client: TestClient, purged: bool
) -> None:
    """The gap: past the TTL, the same key and body ran the payment again."""
    first = client.post("/pay", headers=KEY)
    assert first.status_code == 201
    with freeze_time(expiry() + timedelta(seconds=1)):
        if purged:
            purge()

        retry = client.post("/pay", headers=KEY)

    assert payments() == 1
    assert_buried(retry)


@pytest.mark.parametrize("purged", [True, False], ids=["purged", "expired"])
def test_another_request_with_a_buried_key_is_still_a_mismatch(
    client: TestClient, purged: bool
) -> None:
    """The same key with a different request is a 422, as within the TTL."""
    client.post("/pay", headers=KEY)
    with freeze_time(expiry() + timedelta(seconds=1)):
        if purged:
            purge()

        other = client.post("/pay?outcome=declined", headers=KEY)

    assert (other.status_code, other.json()["code"]) == (422, "idempotency_key_reused")
    assert payments() == 1


def test_a_committed_response_replays_until_the_instant_it_expires(
    client: TestClient,
) -> None:
    """At ``expires_at`` the response is still kept; it is gone only after."""
    client.post("/pay", headers=KEY)
    with freeze_time(expiry()):
        purge()
        at_expiry = client.post("/pay", headers=KEY)

    assert (at_expiry.status_code, at_expiry.content) == (201, b"paid")
    assert at_expiry.headers["Idempotent-Replayed"] == "true"
    assert row_of("pay-ann")["tombstone"] is False


def test_the_purge_keeps_a_tombstone_until_it_expires_in_turn(
    client: TestClient,
) -> None:
    """Without its response and headers, from when the purge made it."""
    client.post("/pay", headers=KEY)
    before = row_of("pay-ann")
    assert (before["body"], before["headers"]) == (b"paid", {})
    later = expiry() + timedelta(minutes=30)  # The hourly purge comes late.

    with freeze_time(later):
        purge()
    buried = row_of("pay-ann")
    with freeze_time(buried["expires_at"]):
        purge()
    kept = row_of("pay-ann")
    with freeze_time(buried["expires_at"] + timedelta(microseconds=1)):
        purge()

    assert buried["tombstone"] is True
    assert (buried["body"], buried["headers"], buried["subjects"]) == (None,) * 3
    assert buried["expires_at"] == later + TOMBSTONE
    for unchanged in ("fingerprint", "status_code", "created_at", "committed_at"):
        assert buried[unchanged] == before[unchanged], unchanged
    assert kept == buried
    assert rows() == []


@pytest.mark.parametrize("purged", [True, False], ids=["purged", "expired"])
def test_a_key_runs_again_once_its_tombstone_expires(
    client: TestClient, purged: bool
) -> None:
    """``TTL_SECONDS`` and ``TOMBSTONE_SECONDS`` later, the key is forgotten."""
    client.post("/pay", headers=KEY)
    with freeze_time(expiry() + timedelta(seconds=1)):
        purge()
    with freeze_time(expiry() + timedelta(seconds=1)):
        if purged:
            purge()
            assert rows() == []

        again = client.post("/pay", headers=KEY)

    assert (again.status_code, again.content) == (201, b"paid")
    assert "Idempotent-Replayed" not in again.headers
    assert payments() == 2
    assert row_of("pay-ann")["tombstone"] is False


def test_requests_that_committed_nothing_run_again_and_leave_no_tombstone(
    client: TestClient,
) -> None:
    """Past the TTL, a key nothing committed or pinned is forgotten, as before."""
    client.post("/counter", json={}, headers={"Idempotency-Key": "count"})
    with freeze_time(expiry("count") + timedelta(seconds=1)):
        again = client.post("/counter", json={}, headers={"Idempotency-Key": "count"})
        purge()  # Leaves the new row alone.

    assert again.json() == {"count": 2}
    assert row_of("count")["tombstone"] is False
    with freeze_time(expiry("count") + timedelta(seconds=1)):
        purge()
    assert rows() == []


def test_a_pinned_key_past_its_ttl_is_not_run_again(client: TestClient) -> None:
    """Its commit on another database may have landed: the key is buried too."""
    plant("pinned", pinned=True)  # Its request died after the pin.

    expired = client.post("/pay", headers={"Idempotency-Key": "pinned"})
    purge()
    buried = client.post("/pay", headers={"Idempotency-Key": "pinned"})

    assert_buried(expired)
    assert_buried(buried)
    assert payments() == 0
    assert row_of("pinned")["tombstone"] is True


def test_a_tombstone_holds_nothing_erasure_or_an_export_would_find(
    client: TestClient,
) -> None:
    """Its response, headers, and whom it was about go when the TTL passes.

    Until the purge comes, an expired response is still found, and erased.
    """
    tag = f"{SEPARATOR}ann{SEPARATOR}{SEPARATOR}"
    for key in ("found", "erased"):
        plant(
            key,
            status_code=201,
            headers={"location": "/patients/1"},
            body=b'{"email": "ann@example.com"}',
            committed_at=datetime.now(UTC),
            subjects=tag,
        )
    found = run(operations.export_subject("default", "ann"))
    erased = run(operations.erase_subject("default", "ann"))
    plant(
        "buried",
        status_code=201,
        headers={"location": "/patients/1"},
        body=b'{"email": "ann@example.com"}',
        committed_at=datetime.now(UTC),
        subjects=tag,
    )

    purge()

    assert [response["withheld"] for response in found] == [False, False]
    assert erased == 2
    assert run(operations.export_subject("default", "ann")) == []
    assert run(operations.erase_subject("default", "ann")) == 0
    for row in rows():
        assert row["tombstone"] is True
        assert (row["body"], row["headers"], row["subjects"]) == (None,) * 3
    assert_buried(client.post("/pay", headers={"Idempotency-Key": "buried"}))


def test_tombstones_are_dated_on_the_database_clock(client: TestClient) -> None:
    """``TOMBSTONE_SECONDS`` from when the purge runs, by the keys' database."""
    client.post("/pay", headers=KEY)

    async def expire() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(idempotency_keys).values(
                    expires_at=datetime.now(UTC) - timedelta(seconds=1)
                )
            )

    run(expire())
    before = datetime.now(UTC)
    purge()
    after = datetime.now(UTC)

    kept_until = expiry()
    slack = timedelta(minutes=1)  # Between the host's clock and the server's.
    assert before + TOMBSTONE - slack <= kept_until <= after + TOMBSTONE + slack
    assert_buried(client.post("/pay", headers=KEY))


@pytest.mark.parametrize("purged", [True, False], ids=["purged", "expired"])
def test_without_tombstones_a_committed_key_runs_again_as_in_0_10(
    database_url: str, purged: bool
) -> None:
    """``TOMBSTONE_SECONDS = 0``: forgotten with its response, after the TTL."""
    with application(database_url, TOMBSTONE_SECONDS=0) as client:
        client.post("/pay", headers=KEY)
        with freeze_time(expiry() + timedelta(seconds=1)):
            if purged:
                purge()
                assert rows() == []

            again = client.post("/pay", headers=KEY)

        assert (again.status_code, again.content) == (201, b"paid")
        assert payments() == 2


def test_without_tombstones_the_purge_deletes_what_it_finds_expired(
    database_url: str,
) -> None:
    """Committed keys and tombstones made before alike, as 0.10's purge."""
    with application(database_url, TOMBSTONE_SECONDS=0):
        plant("committed", committed_at=datetime.now(UTC), status_code=201)
        plant("tombstone", committed_at=datetime.now(UTC), tombstone=True)
        plant("live", expires_at=datetime.now(UTC) + timedelta(hours=1))

        purge()

        assert [row["key_hash"] for row in rows()] == [keyed("live")]


def test_the_shortest_tombstone_still_keeps_the_key(database_url: str) -> None:
    """Any ``TOMBSTONE_SECONDS`` above 0 buries; only 0 forgets."""
    with application(database_url, TOMBSTONE_SECONDS=0.5) as client:
        client.post("/pay", headers=KEY)
        with freeze_time(expiry() + timedelta(seconds=1)) as frozen:
            purge()
            retry = client.post("/pay", headers=KEY)
            frozen.tick(1)  # Past the tombstone's half second.
            again = client.post("/pay", headers=KEY)

        assert_buried(retry)
        assert again.status_code == 201
        assert payments() == 2


def test_without_a_worker_claims_bury_expired_keys_a_batch_at_a_time(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep deletes, then buries; a whole batch of either sweeps again soon."""
    monkeypatch.setattr(idempotency, "MESSAGING", "not.installed")
    monkeypatch.setattr(idempotency, "PURGE_BATCH", 2)
    now = datetime.now(UTC)
    for key in ("paid-1", "paid-2", "paid-3"):
        plant(key, committed_at=now, status_code=201, body=b"paid", headers={})
    plant("abandoned")
    plant("forgotten", committed_at=now, tombstone=True)
    plant("buried", committed_at=now, tombstone=True, expires_at=now + TOMBSTONE)

    client.post("/counter", json={}, headers={"Idempotency-Key": "a"})
    first = {row["key_hash"]: row["tombstone"] for row in rows()}
    client.post("/counter", json={}, headers={"Idempotency-Key": "b"})
    second = {row["key_hash"]: row["tombstone"] for row in rows()}

    paid = [keyed(key) for key in ("paid-1", "paid-2", "paid-3")]
    assert keyed("abandoned") not in first
    assert keyed("forgotten") not in first
    assert sorted(first[key] for key in paid) == [False, True, True]
    assert [second[key] for key in paid] == [True, True, True]
    assert second[keyed("buried")] is True


def test_a_whole_batch_buried_sweeps_again_at_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when the delete found fewer: more tombstones may wait."""
    monkeypatch.setattr(idempotency, "MESSAGING", "not.installed")
    monkeypatch.setattr(idempotency, "PURGE_BATCH", 1)
    now = datetime.now(UTC)
    for key in ("paid-1", "paid-2"):
        plant(key, committed_at=now)

    client.post("/counter", json={}, headers={"Idempotency-Key": "a"})
    client.post("/counter", json={}, headers={"Idempotency-Key": "b"})

    assert [row_of(key)["tombstone"] for key in ("paid-1", "paid-2")] == [True, True]


def test_claims_racing_the_purge_never_take_a_committed_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two replicas claim an expired paid key, each sweeping, beside the purge.

    One row per key: whichever buries it, no claim replaces it, and every
    claim reads it as its first request left it.
    """
    monkeypatch.setattr(idempotency, "SWEEP_EVERY", 0.0)  # Every claim sweeps.
    monkeypatch.setattr(idempotency, "MESSAGING", "not.installed")
    plant("paid", committed_at=datetime.now(UTC), status_code=201, body=b"paid")
    fingerprint = row_of("paid")["fingerprint"]

    async def race() -> list[dict[str, Any] | None]:
        replicas = [
            idempotency.IdempotencyMiddleware(app=None),  # type: ignore[arg-type]
            idempotency.IdempotencyMiddleware(app=None),  # type: ignore[arg-type]
        ]

        async def claim(number: int) -> dict[str, Any] | None:
            operation = Operation(keyed("paid"), f"{number:032d}")
            return await replicas[number % 2]._claim(
                IdempotencySettings(), operation, fingerprint
            )

        async with asyncio.timeout(30):
            *claims, _ = await asyncio.gather(
                *(claim(number) for number in range(5)), purge_expired()
            )
        return claims

    claims = run(race())

    assert [claimed and claimed["token"] for claimed in claims] == ["first"] * 5
    row = row_of("paid")
    assert (row["token"], row["tombstone"], row["body"]) == ("first", True, None)
    assert_buried(client.post("/pay", headers={"Idempotency-Key": "paid"}))


@pytest.mark.parametrize(
    ("row", "buried"),
    [
        ({}, False),
        ({"tombstone": True}, True),
        ({"tombstone": False, "committed_at": "then", idempotency.EXPIRED: True}, True),
        ({"pinned": True, idempotency.EXPIRED: True}, True),
        ({"committed_at": None, "pinned": False, idempotency.EXPIRED: True}, False),
        ({"committed_at": "then", "pinned": True, idempotency.EXPIRED: False}, False),
    ],
)
def test_only_keys_that_may_have_committed_are_buried(
    row: dict[str, Any], buried: bool
) -> None:
    """A key that expired between the claim and the read of its row, unmarked, is not.

    The claim saw it live: it is answered as a live key, and the next
    claim replaces it.
    """
    assert idempotency._buried(row) is buried


def test_tombstones_are_kept_a_week_by_default_and_at_most_ninety_days() -> None:
    """0 turns them off; the bounds are checked at startup."""
    week, ninety = TOMBSTONE.total_seconds(), timedelta(days=90).total_seconds()
    assert [
        IdempotencySettings(**given).TOMBSTONE_SECONDS
        for given in ({}, {"TOMBSTONE_SECONDS": 0}, {"TOMBSTONE_SECONDS": ninety})
    ] == [week, 0, ninety]
    for wrong in (-1, ninety + 1, float("inf")):
        with pytest.raises(ValidationError, match="TOMBSTONE_SECONDS"):
            IdempotencySettings(TOMBSTONE_SECONDS=wrong)
