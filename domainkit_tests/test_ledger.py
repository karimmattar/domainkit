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
        self.histories: list[tuple[int, int, bool]] = []
        self.shutdown_threads: list[str] = []
        self._active = 0
        self.peak = 0

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

    def history(
        self, key: bytes, offset: int, limit: int, sortorder: bool
    ) -> list[Any]:
        """Revisions from ``offset``, at most ``limit``."""
        self._call("history")
        self.histories.append((offset, limit, sortorder))
        if key not in self.values:
            raise RpcError("key not found")
        window = self.values[key][offset : offset + limit]
        return [SimpleNamespace(value=value) for value in window]

    def scan(
        self, key: bytes, prefix: bytes, desc: bool, limit: int
    ) -> dict[bytes, bytes]:
        """Keys with ``prefix``; remembers the arguments asked for."""
        self._call(f"scan:{limit}")
        self.scans.append((key, prefix, desc, limit))
        return {
            k: v[-1] for k, v in sorted(self.values.items()) if k.startswith(prefix)
        }

    def currentState(self) -> Any:  # noqa: ANN401 - SDK API.
        """The server's state."""
        self._call("state")
        return SimpleNamespace(txId=len(self.values))

    def shutdown(self) -> None:
        """Close the channel."""
        self.shutdown_threads.append(threading.current_thread().name)
        self.closed = True


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
        "set",
        "set",
        "get",
        "history",
        "history",
        f"scan:{SCAN_LIMIT}",
        "state",
    ]
    assert client.scans == [(b"", b"k/", False, SCAN_LIMIT)]
    assert [thread.startswith("domainkit-ledger") for thread in client.threads] == [
        True
    ]
    assert client.shutdown_threads[0].startswith("domainkit-ledger")
    assert connects == [ledger.config]
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
    assert client.calls == ["create", "create"]
    assert client.values == {b"k": [b"one"]}


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
    """revision_count reads at most ``limit`` revisions from the start."""
    client = FakeClient()
    ledger, _ = ledger_on(client)
    for value in (b"a", b"b", b"c"):
        await ledger.append(b"k", value)

    counts = [await ledger.revision_count(b"k", limit=n) for n in (1, 2, 5)]
    await ledger.close()

    assert counts == [1, 2, 3]
    # Oldest first, from the first revision.
    assert client.histories == [(0, 1, False), (0, 2, False), (0, 5, False)]


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
    """Unavailable drops the client; later calls connect again."""
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
    assert len(connects) == 2


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
    """Credentials are URL-decoded; port and database have defaults."""
    assert address_of(immudb_config()) == Address(
        "ledger.internal:3323", "writer", "p@ss", "evidence"
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
