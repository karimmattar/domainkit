"""Ledgers: the memory adapter, the immudb adapter on a fake client, the handler."""

import asyncio
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import immudb
import pytest
from ecdsa.keys import BadSignatureError
from immudb.exceptions import ErrCorruptedData, ErrKeyNotFound

from domainkit.conf import LedgerConfig, override_settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.ledger import (
    LedgerChanged,
    LedgerEntry,
    LedgerHandler,
    LedgerKeyExists,
    LedgerKeyNotFound,
    LedgerTampered,
    LedgerUnavailable,
    LedgerWrite,
    MemoryLedger,
    ledgers,
)
from domainkit.ledger.immudb import (
    SCAN_LIMIT,
    Address,
    ImmudbLedger,
    address_of,
    connect,
    translate,
    verified_append_after,
    verified_create,
)

pytestmark = pytest.mark.anyio

SQLITE = {"URL": "sqlite+aiosqlite:///:memory:"}


def immudb_config(**options: Any) -> LedgerConfig:  # noqa: ANN401 - config values.
    """An immudb ledger entry."""
    return LedgerConfig.model_validate(
        {"URL": "immudb://writer:p%40ss@ledger.internal:3323/evidence", **options}
    )


class RpcError(Exception):
    """Looks like a ``grpc.RpcError``: ``details()`` explains the failure."""

    def __init__(self, details: str) -> None:
        """Keep the server's explanation."""
        super().__init__(details)
        self._details = details

    def details(self) -> str:
        """The server's explanation."""
        return self._details

    def __str__(self) -> str:
        """Like grpc's errors, the text is not the details alone."""
        return "<RpcError>"


class FakeClient:
    """Records calls; behaves like immudb for a handful of keys."""

    def __init__(self) -> None:
        """Start empty and connected."""
        self.values: dict[bytes, list[bytes]] = {}
        self.calls: list[str] = []
        self.threads: set[str] = set()
        self.closed = False
        self.fail_with: Exception | None = None
        self.scans: list[tuple[bytes, bytes, bool, int]] = []
        self.state = SimpleNamespace(
            db="evidence", txId=0, txHash=b"\0" * 32, publicKey=b"", signature=b""
        )
        """The server's state; the verified calls here never move it."""
        self.shutdown_threads: list[str] = []
        self._active = 0
        self.peak = 0
        self.shut = threading.Event()

    def _call(self, name: str) -> None:
        self.calls.append(name)
        self.threads.add(threading.current_thread().name)
        self._active += 1
        self.peak = max(self.peak, self._active)
        time.sleep(0.001)  # long enough for overlapping calls to show
        self._active -= 1
        if self.fail_with is not None:
            raise self.fail_with

    def login(self, username: str, password: str, database: bytes) -> object:
        """Not called by the adapter itself (connect logs in)."""
        raise AssertionError("unused")  # pragma: no cover

    def verifiedSet(self, key: bytes, value: bytes) -> Any:  # noqa: ANN401 - SDK API.
        """Append a revision; the id is the revision count."""
        self._call("set")
        self.values.setdefault(key, []).append(value)
        return SimpleNamespace(
            id=sum(len(v) for v in self.values.values()), verified=True
        )

    def verifiedCreate(self, key: bytes, value: bytes) -> Any:  # noqa: ANN401 - SDK API.
        """Write a new key; refuse a written one with immudb's words."""
        self._call("create")
        if key in self.values:
            raise RpcError("precondition failed: KeyMustNotExist")
        self.values[key] = [value]
        return SimpleNamespace(
            id=sum(len(v) for v in self.values.values()), verified=True
        )

    def verifiedAppendAfter(self, key: bytes, value: bytes, transaction: int) -> Any:  # noqa: ANN401 - SDK API.
        """Append unless a revision came after ``transaction``, as immudb refuses."""
        self._call("append-after")
        revisions = self.values.setdefault(key, [])
        if revisions and 10 + len(revisions) > transaction:  # verifiedGet's ids.
            raise RpcError("precondition failed: key modified after given tx")
        revisions.append(value)
        return SimpleNamespace(id=10 + len(revisions), verified=True)

    def verifiedGet(self, key: bytes, atRevision: int | None = None) -> Any:  # noqa: ANN401 - SDK API.
        """Return a revision, or fail like immudb does."""
        self._call("get")
        revisions = self.values.get(key)
        if not revisions:
            raise RpcError("tbtree: key not found")
        number = atRevision or len(revisions)
        return SimpleNamespace(
            id=10 + number, value=revisions[number - 1], revision=number
        )

    def scan(
        self, key: bytes, prefix: bytes, desc: bool, limit: int
    ) -> dict[bytes, bytes]:
        """Keys with ``prefix``; remembers the arguments asked for."""
        self._call(f"scan:{limit}")
        self.scans.append((key, prefix, desc, limit))
        return {
            k: v[-1] for k, v in sorted(self.values.items()) if k.startswith(prefix)
        }

    def serverState(self) -> Any:  # noqa: ANN401 - SDK API.
        """The server's state."""
        self._call("state")
        return self.state

    def shutdown(self) -> None:
        """Close the channel."""
        self.shutdown_threads.append(threading.current_thread().name)
        self.closed = True
        self.shut.set()


def ledger_on(
    client: FakeClient,
    **options: Any,  # noqa: ANN401 - config values.
) -> tuple[ImmudbLedger, list[LedgerConfig]]:
    """An ImmudbLedger whose factory returns ``client`` and records each connect."""
    connects: list[LedgerConfig] = []

    def factory(config: LedgerConfig) -> FakeClient:
        connects.append(config)
        client.closed = False
        return client

    return ImmudbLedger(immudb_config(**options), factory=factory), connects


# MemoryLedger ------------------------------------------------------------------


async def test_memory_ledgers_keep_every_revision() -> None:
    """Writes add revisions; reads pick the latest or a given one."""
    ledger = MemoryLedger()

    first = await ledger.append(b"a/1", b"one")
    second = await ledger.append(b"a/1", b"two")
    await ledger.append(b"b/1", b"other")

    assert first == LedgerWrite(b"a/1", 1)
    assert second.transaction == 2
    assert await ledger.read(b"a/1") == LedgerEntry(b"a/1", b"two", 2, 2)
    assert await ledger.read(b"a/1", revision=1) == LedgerEntry(b"a/1", b"one", 1, 1)
    assert await ledger.revision_count(b"a/1", limit=5) == 2
    assert await ledger.revision_count(b"a/1", limit=1) == 1
    assert await ledger.revision_count(b"none", limit=5) == 0
    assert await ledger.keys(b"a/", limit=10) == [b"a/1"]
    assert await ledger.keys(b"", limit=1) == [b"a/1"]
    await ledger.check()
    await ledger.close()
    for key, revision, number in ((b"none", None, 0), (b"a/1", 3, 3), (b"a/1", 0, 0)):
        with pytest.raises(LedgerKeyNotFound) as missing:
            await ledger.read(key, revision=revision)
        assert str(missing.value) == f"No revision {number} of {key!r}."


async def test_memory_ledgers_append_only_after_what_was_read() -> None:
    """``unchanged_since`` refuses a write once a later revision exists."""
    ledger = MemoryLedger()

    first = await ledger.append(b"k", b"one", unchanged_since=0)
    second = await ledger.append(b"k", b"two", unchanged_since=first.transaction)
    with pytest.raises(LedgerChanged, match="changed after 1"):
        await ledger.append(b"k", b"late", unchanged_since=first.transaction)
    with pytest.raises(LedgerChanged, match="changed after 0"):
        await ledger.append(b"k", b"late", unchanged_since=0)

    assert second.transaction == 2
    assert await ledger.revision_count(b"k", limit=5) == 2


async def test_memory_ledgers_create_only_new_keys() -> None:
    """A create writes revision 1 of a new key and refuses a written one."""
    ledger = MemoryLedger()

    created = await ledger.create(b"k", b"one")
    with pytest.raises(LedgerKeyExists, match="already written"):
        await ledger.create(b"k", b"two")

    assert created == LedgerWrite(b"k", 1)
    assert await ledger.read(b"k") == LedgerEntry(b"k", b"one", 1, 1)


# ImmudbLedger on a fake client -------------------------------------------------


async def test_immudb_ledgers_call_the_sdk_on_their_own_thread() -> None:
    """Every call runs on one ledger thread, never on the event loop's."""
    client = FakeClient()
    ledger, connects = ledger_on(client)

    written = await ledger.append(b"k/1", b"v")
    await ledger.append(b"k/1", b"w")
    entry = await ledger.read(b"k/1", revision=1)
    count = await ledger.revision_count(b"k/1", limit=1)
    missing = await ledger.revision_count(b"none", limit=2)
    keys = await ledger.keys(b"k/", limit=5000)
    await ledger.check()
    await ledger.close()
    await ledger.close()

    assert written == LedgerWrite(b"k/1", 1)
    assert entry == LedgerEntry(b"k/1", b"v", 11, 1)
    assert (count, missing, keys) == (1, 0, [b"k/1"])
    assert client.calls == [
        "state",  # The work's client joins: the server's state is checked.
        "set",
        "set",
        "get",
        "get",  # Counts are verified reads of the latest revision.
        "get",
        f"scan:{SCAN_LIMIT}",
        "state",  # The probe's client joins.
        "state",
    ]
    assert client.scans == [(b"", b"k/", False, SCAN_LIMIT)]
    # The work on its thread; the readiness probe on one of its own.
    assert client.threads == {"domainkit-ledger_0", "domainkit-ledger-check_0"}
    assert sorted(client.shutdown_threads) == sorted(client.threads)
    assert connects == [ledger.config, ledger.config]
    assert client.closed


async def test_immudb_creates_are_conditional_sets() -> None:
    """A create is the conditional set; immudb's refusal is LedgerKeyExists."""
    client = FakeClient()
    ledger, _ = ledger_on(client)

    created = await ledger.create(b"k", b"one")
    with pytest.raises(LedgerKeyExists, match="already has this key"):
        await ledger.create(b"k", b"two")
    await ledger.close()

    assert created == LedgerWrite(b"k", 1)
    assert client.calls == ["state", "create", "create"]
    assert client.values == {b"k": [b"one"]}


async def test_immudb_appends_after_a_revision_are_conditional() -> None:
    """``unchanged_since`` is immudb's precondition; its refusal is LedgerChanged."""
    client = FakeClient()
    ledger, _ = ledger_on(client)

    await ledger.append(b"k", b"one")
    read = await ledger.read(b"k")
    after = await ledger.append(b"k", b"two", unchanged_since=read.transaction)
    with pytest.raises(LedgerChanged) as late:
        await ledger.append(b"k", b"late", unchanged_since=read.transaction)
    fresh = await ledger.append(b"new", b"one", unchanged_since=0)
    with pytest.raises(LedgerChanged):
        await ledger.append(b"new", b"again", unchanged_since=0)
    await ledger.close()

    assert (after, fresh) == (LedgerWrite(b"k", 12), LedgerWrite(b"new", 11))
    assert str(late.value) == "The ledger has a later revision of this key."
    assert isinstance(late.value.__cause__, LedgerKeyExists)
    assert client.values == {b"k": [b"one", b"two"], b"new": [b"one"]}


class HeldClient(FakeClient):
    """A client whose writes wait until ``resume`` is set."""

    def __init__(self) -> None:
        """Start empty, with writes held."""
        super().__init__()
        self.entered = threading.Event()
        self.resume = threading.Event()

    def verifiedSet(self, key: bytes, value: bytes) -> Any:  # noqa: ANN401 - SDK API.
        """Hold the write, as a slow ledger does."""
        self.entered.set()
        self.resume.wait(30)
        return super().verifiedSet(key, value)


async def test_readiness_never_waits_behind_the_ledgers_work() -> None:
    """A probe answers while a write still holds the work thread."""
    client = HeldClient()
    ledger, _ = ledger_on(client)
    writing = asyncio.create_task(ledger.append(b"k", b"v"))
    try:
        assert await asyncio.to_thread(client.entered.wait, 10)

        await ledger.check()

        assert not writing.done()
    finally:
        client.resume.set()
    assert (await writing).transaction == 1
    await ledger.close()


async def test_closing_a_busy_ledger_waits_at_most_its_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A call that outlived its caller holds the client: close returns anyway."""
    client = HeldClient()
    ledger, _ = ledger_on(client, TIMEOUT=0.05)
    with pytest.raises(LedgerUnavailable):
        await ledger.append(b"k", b"v")  # Given up on; the write still runs.
    assert client.entered.is_set()

    await ledger.close()

    assert not client.closed
    assert (
        "The ledger's domainkit-ledger thread is busy; its client closes when "
        "the call returns."
    ) in caplog.messages
    client.resume.set()
    assert await asyncio.to_thread(client.shut.wait, 10)
    assert client.values == {b"k": [b"v"]}  # The late write landed, then it closed.


async def test_a_close_cut_short_still_closes_the_client() -> None:
    """When the caller's budget ends first, the client closes once it is free."""
    client = HeldClient()
    ledger, _ = ledger_on(client)
    writing = asyncio.create_task(ledger.append(b"k", b"v"))
    assert await asyncio.to_thread(client.entered.wait, 10)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await ledger.close()

    client.resume.set()
    assert (await writing).transaction == 1
    assert await asyncio.to_thread(client.shut.wait, 10)


def test_connect_hands_the_sdk_its_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The SDK client gets the target, signing key, and timeout, then logs in."""
    made: list[Any] = []

    class Sdk:
        def __init__(
            self, target: str, publicKeyFile: str | None, timeout: float
        ) -> None:
            made.append((target, publicKeyFile, timeout))
            self.logins: list[tuple[str, str, bytes]] = []

        def login(self, username: str, password: str, database: bytes) -> None:
            self.logins.append((username, password, database))

    monkeypatch.setattr(immudb, "ImmudbClient", Sdk)
    signing = tmp_path / "signing.pub"
    plain = connect(immudb_config(TIMEOUT=7))
    connect(immudb_config(SERVER_SIGNING_KEY=signing))

    assert made == [
        ("ledger.internal:3323", None, 7.0),
        ("ledger.internal:3323", str(signing), 10.0),
    ]
    assert plain.logins == [("writer", "p@ss", b"evidence")]  # type: ignore[attr-defined]
    create = plain.verifiedCreate
    assert (create.func, create.args) == (verified_create, (plain,))  # type: ignore[attr-defined]
    after = plain.verifiedAppendAfter
    assert (after.func, after.args) == (verified_append_after, (plain,))  # type: ignore[attr-defined]


def test_conditional_sets_carry_their_precondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each conditional write reaches immudb with its precondition, then verifies."""
    from immudb.grpc import schema_pb2
    from immudb.handler import verifiedSet

    sent: list[Any] = []

    class Stub:
        def VerifiableSet(self, request: Any) -> str:  # noqa: ANN401 - gRPC API.
            sent.append(request)
            return "verified"

    def call(service: Any, state: str, key: bytes, value: bytes, signing: str) -> Any:  # noqa: ANN401 - SDK API.
        assert (state, signing) == ("trusted state", "signing key")
        kv = schema_pb2.KeyValue(key=key, value=value)
        request = schema_pb2.VerifiableSetRequest(
            setRequest=schema_pb2.SetRequest(KVs=[kv])
        )
        return service.VerifiableSet(request)

    monkeypatch.setattr(verifiedSet, "call", call)
    client = SimpleNamespace(_stub=Stub(), _rs="trusted state", _vk="signing key")

    results = [
        verified_create(client, b"k", b"first"),
        verified_append_after(client, b"k", b"second", 0),
        verified_append_after(client, b"k", b"third", 42),
    ]

    assert results == ["verified"] * 3
    assert [
        [(kv.key, kv.value) for kv in request.setRequest.KVs] for request in sent
    ] == [[(b"k", b"first")], [(b"k", b"second")], [(b"k", b"third")]]
    (new,), (still_new,), (unchanged,) = (
        list(request.setRequest.preconditions) for request in sent
    )
    assert new.keyMustNotExist.key == still_new.keyMustNotExist.key == b"k"
    assert unchanged.WhichOneof("precondition") == "keyNotModifiedAfterTX"
    after = unchanged.keyNotModifiedAfterTX
    assert (after.key, after.txID) == (b"k", 42)


def test_connect_names_the_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without immudb-py installed, the error says which extra to install."""
    monkeypatch.setitem(sys.modules, "immudb", None)

    with pytest.raises(ImproperlyConfigured) as missing:
        connect(immudb_config())

    assert str(missing.value) == (
        "immudb ledgers need the immudb extra: pip install 'domainkit[immudb]'."
    )


async def test_calls_never_overlap() -> None:
    """The SDK keeps its trusted state in the client: one call at a time."""
    client = FakeClient()
    ledger, _ = ledger_on(client)

    await asyncio.gather(*(ledger.check() for _ in range(20)))
    await ledger.close()

    assert client.peak == 1


async def test_counts_stop_at_the_limit() -> None:
    """revision_count is the latest revision's number, at most ``limit``."""
    client = FakeClient()
    ledger, _ = ledger_on(client)
    for value in (b"a", b"b", b"c"):
        await ledger.append(b"k", value)

    counts = [await ledger.revision_count(b"k", limit=n) for n in (1, 2, 5)]
    await ledger.close()

    assert counts == [1, 2, 3]
    # The latest revision, read verified: never the SDK's unverified history.
    assert client.calls[-3:] == ["get", "get", "get"]


class SilentRpcError(Exception):
    """A gRPC error whose ``details()`` is empty."""

    def details(self) -> None:
        """Nothing to explain."""


NOT_FOUND = "The ledger has no such key or revision."
TAMPERED = "A ledger proof or server signature did not verify; do not trust it."


@pytest.mark.parametrize(
    ("error", "raised", "message"),
    [
        (ErrCorruptedData(), LedgerTampered, TAMPERED),
        (BadSignatureError("Signature verification failed"), LedgerTampered, TAMPERED),
        (RpcError("tbtree: key not found"), LedgerKeyNotFound, NOT_FOUND),
        (RpcError("invalid key revision number"), LedgerKeyNotFound, NOT_FOUND),
        (
            RpcError("precondition failed: KeyMustNotExist"),
            LedgerKeyExists,
            "The ledger already has this key.",
        ),
        (ErrKeyNotFound(), LedgerKeyNotFound, NOT_FOUND),
        (RuntimeError("tbtree: key not found"), LedgerKeyNotFound, NOT_FOUND),
        (
            RpcError("connection refused"),
            LedgerUnavailable,
            "The ledger call failed (RpcError).",
        ),
        (OSError("no route"), LedgerUnavailable, "The ledger call failed (OSError)."),
        (
            SilentRpcError(),
            LedgerUnavailable,
            "The ledger call failed (SilentRpcError).",
        ),
    ],
)
def test_sdk_errors_become_ledger_errors(
    error: Exception, raised: type[Exception], message: str
) -> None:
    """Proof and signature failures are tampering; the rest say what failed."""
    translated = translate(error)

    assert isinstance(translated, raised)
    assert str(translated) == message


async def test_failed_calls_reconnect_and_keep_ledger_errors() -> None:
    """Unavailable drops the client; later calls connect again.

    A client that cannot join (the server's state fails its check) is
    closed and not kept: the next call connects again.
    """
    client = FakeClient()
    ledger, connects = ledger_on(client)
    await ledger.check()

    client.fail_with = RpcError("connection reset")
    with pytest.raises(LedgerUnavailable, match="RpcError"):
        await ledger.check()
    closed_after_failure = client.closed
    client.fail_with = LedgerTampered("already translated")
    with pytest.raises(LedgerTampered, match="already translated"):
        await ledger.check()
    client.fail_with = None
    await ledger.check()
    await ledger.close()

    assert closed_after_failure
    assert len(connects) == 3


async def test_slow_ledgers_time_out() -> None:
    """A call past TIMEOUT is unavailable; the event loop never waits longer."""
    release = threading.Event()

    def factory(config: LedgerConfig) -> Any:  # noqa: ANN401 - a stand-in client.
        release.wait(5)
        return FakeClient()

    ledger = ImmudbLedger(immudb_config(TIMEOUT=0.05), factory=factory)
    try:
        with pytest.raises(LedgerUnavailable, match=r"within 0\.05s"):
            await ledger.check()
    finally:
        release.set()
        await ledger.close()


def test_immudb_urls_name_server_user_and_database() -> None:
    """Credentials are URL-decoded; port and database have defaults.

    The password is not in the address's ``repr`` (SEC-r1-6).
    """
    address = address_of(immudb_config())
    assert address == Address("ledger.internal:3323", "writer", "p@ss", "evidence")
    assert repr(address) == (
        "Address(target='ledger.internal:3323', user='writer', database='evidence')"
    )
    assert address_of(
        LedgerConfig.model_validate({"URL": "immudb://ledger.internal"})
    ) == Address("ledger.internal:3322", "", "", "defaultdb")
    assert (
        address_of(
            LedgerConfig.model_validate({"URL": "immudb://ledger.internal/Xevidence"})
        ).database
        == "Xevidence"
    )


# The handler -------------------------------------------------------------------


async def test_ledgers_come_from_databases_by_alias() -> None:
    """One ledger per alias and configuration, closed together."""
    built: list[str] = []

    def memory(config: LedgerConfig) -> MemoryLedger:
        built.append(config.URL.get_secret_value())
        return MemoryLedger()

    handler = LedgerHandler({"memory": memory, "immudb": memory})
    with override_settings(
        DATABASES={"default": SQLITE}, LEDGERS={"evidence": {"URL": "memory://"}}
    ):
        first = handler["evidence"]
        again = handler["evidence"]
        assert await handler.check_all(1.0) == []
        with pytest.raises(ImproperlyConfigured) as sql:
            handler["default"]
        with pytest.raises(ImproperlyConfigured) as unknown:
            handler["nowhere"]
        assert str(sql.value) == "LEDGERS has no 'default' alias; it is a SQL database."
        assert str(unknown.value) == "LEDGERS has no 'nowhere' alias."
    with override_settings(
        DATABASES={"default": SQLITE},
        LEDGERS={"evidence": {"URL": "memory://", "TIMEOUT": 1}},
    ):
        changed = handler["evidence"]
    await handler.close_all()

    assert first is again
    assert changed is not first
    assert built == ["memory://", "memory://"]


async def test_closing_the_ledgers_closes_each_ones_clients() -> None:
    """``close_all`` runs each ledger's own ``close``, not the base class's."""
    closed: list[str] = []

    class Closing(MemoryLedger):
        async def close(self) -> None:
            closed.append("closed")

    handler = LedgerHandler({"memory": lambda config: Closing()})
    with override_settings(
        DATABASES={"default": SQLITE},
        LEDGERS={"evidence": {"URL": "memory://"}, "audit": {"URL": "memory://"}},
    ):
        opened = [handler["evidence"], handler["audit"]]
    await handler.close_all()

    assert len(opened) == 2
    assert closed == ["closed", "closed"]


async def test_failing_ledgers_fail_readiness() -> None:
    """Each failing ledger is reported as ledger:<alias>."""

    class Broken(MemoryLedger):
        async def check(self) -> None:
            raise LedgerUnavailable("down")

    handler = LedgerHandler({"memory": lambda config: Broken()})
    with override_settings(
        DATABASES={"default": SQLITE}, LEDGERS={"evidence": {"URL": "memory://"}}
    ):
        assert await handler.check_all(1.0) == ["ledger:evidence"]
    await handler.close_all()


async def test_the_process_handler_builds_memory_ledgers() -> None:
    """``ledgers`` knows both schemes out of the box."""
    with override_settings(
        DATABASES={"default": SQLITE}, LEDGERS={"scratch": {"URL": "memory://"}}
    ):
        assert isinstance(ledgers["scratch"], MemoryLedger)
    await ledgers.close_all()


def test_ready_checks_cover_ledgers(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/ready`` fails when a ledger fails."""
    from fastapi.testclient import TestClient

    from domainkit.http import get_asgi_application

    with override_settings(
        DATABASES={"default": SQLITE},
        LEDGERS={"evidence": {"URL": "memory://"}},
        READINESS_DETAILS=True,
    ):
        app = get_asgi_application()
        with TestClient(app) as client:
            ready = client.get("/ready")
        monkeypatch.setattr(ledgers, "_factories", {"memory": lambda config: Down()})
        with TestClient(app) as client:
            down = client.get("/ready")

    assert ready.status_code == 200
    assert (down.status_code, down.json()["failing"]) == (503, ["ledger:evidence"])


class Down(MemoryLedger):
    """A ledger that is never ready."""

    async def check(self) -> None:
        """Fail the probe."""
        raise LedgerUnavailable("down")


def test_sql_connections_refuse_ledgers() -> None:
    """A ledger alias is never opened as a SQL engine."""
    from domainkit.db import connections

    with (
        override_settings(
            DATABASES={"default": SQLITE}, LEDGERS={"evidence": {"URL": "memory://"}}
        ),
        pytest.raises(
            ImproperlyConfigured,
            match="DATABASES has no 'evidence' alias; it is a ledger",
        ),
    ):
        connections.config("evidence")


async def test_configuration_errors_are_not_disguised() -> None:
    """A missing extra or a bad setting surfaces as itself, not as unavailable."""

    def factory(config: LedgerConfig) -> Any:  # noqa: ANN401 - never returns.
        raise ImproperlyConfigured("install the immudb extra")

    ledger = ImmudbLedger(immudb_config(), factory=factory)
    try:
        with pytest.raises(ImproperlyConfigured, match="install the immudb extra"):
            await ledger.check()
    finally:
        await ledger.close()


async def test_slow_ledgers_fail_readiness_in_time() -> None:
    """A ledger slower than the readiness budget counts as failing."""

    class Slow(MemoryLedger):
        async def check(self) -> None:
            await asyncio.sleep(1)

    handler = LedgerHandler({"memory": lambda config: Slow()})
    with override_settings(
        DATABASES={"default": SQLITE}, LEDGERS={"evidence": {"URL": "memory://"}}
    ):
        assert await handler.check_all(0.05) == ["ledger:evidence"]
    await handler.close_all()
