"""Ledger trust survives reconnects and restarts; a rolled-back ledger is refused.

``FakeServer`` keeps a ledger as immudb does, a chain of transactions each
with a digest, and can be rolled back to an older copy or forked. Its
clients verify as the SDK does: each call proves the server's answer from
the state the client trusts, and fails when the server no longer has that
state. Like the SDK, a client starts by trusting the server's state when it
logs in; the adapter replaces that with the ledger's own (``TrustedRoot``).
"""

import hashlib
import io
import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest
from ecdsa import NIST256p, SigningKey
from ecdsa.keys import BadSignatureError
from immudb.exceptions import ErrCorruptedData

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import LedgerConfig, override_settings
from domainkit.contrib.audit.ledgerstates import DatabaseStateStore
from domainkit.contrib.evidence import Evidence, EvidenceTampered, LedgerEvidenceStore
from domainkit.ledger import (
    LedgerEntry,
    LedgerKeyNotFound,
    LedgerTampered,
    LedgerUnavailable,
    MemoryLedger,
)
from domainkit.ledger.immudb import ImmudbLedger, SdkState, TrustedRoot, server_state
from domainkit.ledger.trust import (
    LedgerRolledBack,
    MemoryStateStore,
    StateStore,
    TrustedState,
    registered_states,
    use_states,
)
from domainkit.management import call_command
from domainkit_tests.conftest import ALPHA

pytestmark = pytest.mark.anyio

URL: Final = "immudb://writer:pw@ledger.internal:3323/evidence"
NAME: Final = "ledger.internal:3323/evidence"


class RpcError(Exception):
    """Looks like a ``grpc.RpcError``."""

    def details(self) -> str:
        """The server's explanation."""
        return str(self.args[0])


class FakeServer:
    """An immudb database: transactions, each a key and value, chained by digest."""

    def __init__(self) -> None:
        """Start with no transactions, at the empty digest."""
        self.entries: list[tuple[bytes, bytes]] = []
        self.digests: list[bytes] = [b"\0" * 32]
        """The digest after each transaction; index 0 before any."""

    @property
    def tx(self) -> int:
        """The newest transaction."""
        return len(self.entries)

    def state(self) -> SimpleNamespace:
        """The server's state, as the SDK reads it."""
        return SimpleNamespace(
            db="evidence",
            txId=self.tx,
            txHash=self.digests[self.tx],
            publicKey=b"",
            signature=b"",
        )

    def write(self, key: bytes, value: bytes) -> int:
        """Append a transaction; return its number."""
        self.entries.append((key, value))
        previous = self.digests[-1]
        self.digests.append(hashlib.sha256(previous + key + b"=" + value).digest())
        return self.tx

    def roll_back(self, to: int) -> None:
        """Restore the copy the server had at transaction ``to``."""
        del self.entries[to:]
        del self.digests[to + 1 :]

    def fork(self, at: int) -> None:
        """Rewrite history from transaction ``at`` on, as many transactions."""
        count = self.tx
        self.roll_back(at - 1)
        while self.tx < count:
            self.write(b"forged", b"x")

    def prove(self, trusted: Any) -> None:  # noqa: ANN401 - an SDK state.
        """Prove the current state follows ``trusted``, as a dual proof does.

        Raises:
            RpcError: The server has no transaction that late (rolled back).
            ErrCorruptedData: Its digest there differs (forked).
        """
        if trusted.txId > self.tx:
            raise RpcError("tx not found")
        if self.digests[trusted.txId] != bytes(trusted.txHash):
            raise ErrCorruptedData


class SdkRoot:
    """The SDK's in-memory ``RootService``: trusts the server's state at login."""

    def __init__(self, state: Any) -> None:  # noqa: ANN401 - an SDK state.
        """Trust ``state``."""
        self.state = state

    def get(self) -> Any:  # noqa: ANN401 - an SDK state.
        """The trusted state."""
        return self.state

    def set(self, state: Any) -> None:  # noqa: ANN401 - an SDK state.
        """Trust ``state``."""
        self.state = state


class FakeClient:
    """An SDK client of ``FakeServer``: every answer proven from ``_rs``."""

    def __init__(self, server: FakeServer) -> None:
        """Log in: trust the server's state, as the SDK's default root does."""
        self.server = server
        self._rs: Any = SdkRoot(server.state())

    def _proven(self) -> None:
        """Prove from the trusted state; then trust the server's."""
        self.server.prove(self._rs.get())
        self._rs.set(self.server.state())

    def verifiedSet(self, key: bytes, value: bytes) -> Any:  # noqa: ANN401 - SDK API.
        """Write, proven."""
        self.server.prove(self._rs.get())
        tx = self.server.write(key, value)
        self._rs.set(self.server.state())
        return SimpleNamespace(id=tx, verified=True)

    def verifiedCreate(self, key: bytes, value: bytes) -> Any:  # noqa: ANN401 - SDK API.
        """Write a new key, proven."""
        return self.verifiedSet(key, value)

    def verifiedAppendAfter(self, key: bytes, value: bytes, transaction: int) -> Any:  # noqa: ANN401 - SDK API.
        """Write, proven."""
        return self.verifiedSet(key, value)

    def verifiedGet(self, key: bytes, atRevision: int | None = None) -> Any:  # noqa: ANN401 - SDK API.
        """Read a revision, proven."""
        self._proven()
        revisions = [
            (tx, value)
            for tx, (written, value) in enumerate(self.server.entries, 1)
            if written == key
        ]
        if not revisions:
            raise RpcError("key not found")
        number = atRevision or len(revisions)
        tx, value = revisions[number - 1]
        return SimpleNamespace(id=tx, value=value, revision=number)

    def scan(
        self, key: bytes, prefix: bytes, desc: bool, limit: int
    ) -> dict[bytes, bytes]:
        """Keys with ``prefix`` (unverified, as the SDK's)."""
        return {k: v for k, v in self.server.entries if k.startswith(prefix)}

    def serverState(self) -> Any:  # noqa: ANN401 - SDK API.
        """The server's state, unproven."""
        return self.server.state()

    def login(self, username: str, password: str, database: bytes) -> object:
        """Not called by the adapter (``connect`` logs in)."""
        raise AssertionError("unused")  # pragma: no cover

    def shutdown(self) -> None:
        """Close."""
        self._rs = None


def config() -> LedgerConfig:
    """The ledger's settings."""
    return LedgerConfig.model_validate({"URL": URL})


def ledger_of(server: FakeServer, states: StateStore | None = None) -> ImmudbLedger:
    """A ledger whose clients talk to ``server``; its states in ``states``."""
    if states is None:  # As 0.9 made them, too.
        return ImmudbLedger(config(), factory=lambda _: FakeClient(server))
    return ImmudbLedger(config(), factory=lambda _: FakeClient(server), states=states)


async def test_a_reconnected_client_still_refuses_a_rolled_back_ledger() -> None:
    """The trusted state is the ledger's: a reconnect does not start over.

    In 0.9 the state lived in the client, which a reconnect replaced: the
    new client trusted whatever the server showed it.
    """
    server = FakeServer()
    ledger = ledger_of(server)
    for value in (b"1", b"2", b"3"):
        await ledger.append(b"k", value)
    server.roll_back(1)
    await ledger.close()  # As after a timeout: the next call reconnects.

    with pytest.raises(
        LedgerRolledBack, match="at transaction 1, before transaction 3"
    ):
        await ledger.read(b"k")
    with pytest.raises(LedgerRolledBack):
        await ledger.check()
    await ledger.close()


async def test_a_new_process_starts_from_the_saved_state() -> None:
    """Another ledger with the same store refuses what the first verified past."""
    server = FakeServer()
    store = MemoryStateStore()
    first = ledger_of(server, store)
    for value in (b"1", b"2", b"3"):
        await first.append(b"k", value)
    await first.close()
    server.roll_back(2)

    restarted = ledger_of(server, store)
    with pytest.raises(LedgerRolledBack):
        await restarted.append(b"k", b"4")
    with pytest.raises(LedgerRolledBack):
        await restarted.check()
    saved = await store.load(NAME)
    await restarted.close()

    assert saved is not None
    assert (saved.database, saved.transaction) == ("evidence", 3)
    assert len(saved.digest) == 32


async def test_a_running_probe_refuses_a_rolled_back_ledger() -> None:
    """The probe's client compares the server's state with the trusted one."""
    server = FakeServer()
    ledger = ledger_of(server)
    await ledger.check()
    await ledger.append(b"k", b"1")
    await ledger.append(b"k", b"2")
    server.roll_back(1)

    with pytest.raises(LedgerRolledBack):
        await ledger.check()
    await ledger.close()


async def test_a_forked_ledger_is_tampering() -> None:
    """The same transaction with another digest: refused by the probe and calls."""
    server = FakeServer()
    ledger = ledger_of(server)
    await ledger.check()
    await ledger.append(b"k", b"1")
    await ledger.append(b"k", b"2")
    server.fork(at=2)

    with pytest.raises(LedgerTampered, match="was forked") as probed:
        await ledger.check()
    with pytest.raises(LedgerTampered, match="did not verify"):
        await ledger.read(b"k")
    await ledger.close()

    assert not isinstance(probed.value, LedgerRolledBack)


async def test_the_probe_never_moves_the_trusted_state_on() -> None:
    """A newer server state is not trusted until a verified call proves it."""
    server = FakeServer()
    ledger = ledger_of(server)
    await ledger.append(b"k", b"1")
    server.write(b"other", b"written by another client")
    await ledger.check()
    probed = ledger.root.get().txId
    await ledger.read(b"k")
    await ledger.close()

    assert (probed, ledger.root.get().txId) == (1, 2)


async def test_the_first_client_trusts_the_first_state() -> None:
    """Nothing saved, nothing trusted: the server's state at first contact is."""
    server = FakeServer()
    server.write(b"k", b"before")
    store = MemoryStateStore()
    ledger = ledger_of(server, store)

    entry = await ledger.read(b"k")
    saved = await store.load(NAME)
    await ledger.close()

    assert entry == LedgerEntry(b"k", b"before", 1, 1)
    assert saved is not None
    assert saved.transaction == 1


class Flaky(MemoryStateStore):
    """A store that fails to save, then works; or fails to load."""

    def __init__(self, *, saves: int = 0, loads: bool = True) -> None:
        """Fail the first ``saves`` saves; fail every load unless ``loads``."""
        super().__init__()
        self.failures = saves
        self.loads = loads

    async def load(self, ledger: str) -> TrustedState | None:
        """Fail unless loading works."""
        if not self.loads:
            raise OSError("the database is down")
        return await super().load(ledger)

    async def save(self, ledger: str, state: TrustedState) -> None:
        """Fail while failures are left."""
        if self.failures:
            self.failures -= 1
            raise OSError("the database is down")
        await super().save(ledger, state)


async def test_a_failed_save_is_logged_and_tried_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The write landed: it is not undone; the state is saved after the next call."""
    server = FakeServer()
    store = Flaky(saves=1)
    ledger = ledger_of(server, store)

    with caplog.at_level(logging.WARNING, "domainkit.ledger.immudb"):
        written = await ledger.append(b"k", b"1")
    unsaved = await store.load(NAME)
    await ledger.read(b"k")
    saved = await store.load(NAME)
    await ledger.close()

    assert written.transaction == 1
    assert unsaved is None
    assert saved is not None
    assert saved.transaction == 1
    assert f"Could not save the state ledger {NAME} is trusted at" in caplog.text


async def test_a_state_that_cannot_be_read_refuses_the_call() -> None:
    """Without the saved state the client would trust anything: nothing is sent."""
    server = FakeServer()
    ledger = ledger_of(server, Flaky(loads=False))

    with pytest.raises(LedgerUnavailable, match="could not be read"):
        await ledger.append(b"k", b"1")
    await ledger.close()

    assert server.tx == 0


def test_the_trusted_state_only_moves_forward() -> None:
    """``set`` takes later states only; ``adopt`` too; login is ignored."""
    root = TrustedRoot()
    late = SdkState("db", 5, b"5" * 32)
    root.set(late)
    root.set(SdkState("db", 4, b"4" * 32))
    root.adopt(TrustedState("db", 3, b"3" * 32))
    root.init("url/db", object())

    assert root.get() is late
    assert root.unsaved() == TrustedState("db", 5, b"5" * 32)
    assert root.unsaved() is None
    root.adopt(TrustedState("db", 9, b"9" * 32, b"sig", b"key"))
    assert root.get() == SdkState("db", 9, b"9" * 32, b"key", b"sig")
    assert root.unsaved() is None  # Taken from the store: nothing to save.
    root.save_failed()
    assert root.unsaved() == TrustedState("db", 9, b"9" * 32, b"sig", b"key")


async def test_memory_stores_never_move_back() -> None:
    """A save of an older state keeps the later one."""
    store = MemoryStateStore()
    await store.save("l", TrustedState("db", 5, b"5" * 32))
    await store.save("l", TrustedState("db", 3, b"3" * 32))

    assert await store.load("l") == TrustedState("db", 5, b"5" * 32)
    assert await store.load("other") is None


@pytest.fixture
def audited(database_url: str, tmp_path: Path) -> Iterator[str]:
    """The audit app on a migrated database."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=["domainkit.contrib.audit", ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        call_command("makemigrations", "-m", "audit", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield database_url
    registry.clear()


@pytest.mark.usefixtures("audited")
async def test_the_audit_database_keeps_states_for_every_process() -> None:
    """Installed, audit keeps them in ``AUDIT.DATABASE``; a row never moves back."""
    registered = registered_states()
    assert isinstance(registered, DatabaseStateStore)
    server = FakeServer()
    first = ledger_of(server)
    await first.append(b"k", b"1")
    await first.append(b"k", b"2")
    await first.close()
    await registered.save(NAME, TrustedState("evidence", 1, b"1" * 32))
    server.roll_back(1)

    with pytest.raises(LedgerRolledBack):
        await ledger_of(server).check()
    kept = await registered.load(NAME)

    assert kept is not None
    assert (kept.database, kept.transaction, len(kept.digest)) == ("evidence", 2, 32)
    assert await registered.load("another") is None


def test_a_registered_store_ends_with_its_domains() -> None:
    """``registry.clear()`` ends a registration: the next settings may differ."""
    store = MemoryStateStore()
    use_states(store)
    assert registered_states() is store
    registry.clear()
    assert registered_states() is None


def test_server_states_are_checked_against_the_signing_key() -> None:
    """The probe's state is the server's, signed; another key's signature fails."""
    from immudb.grpc import schema_pb2

    key = SigningKey.generate(curve=NIST256p)
    state = schema_pb2.ImmutableState(db="evidence", txId=7, txHash=b"h" * 32)
    signed = SdkState("evidence", 7, b"h" * 32)
    message = (
        len(signed.db).to_bytes(4, "big")
        + signed.db.encode()
        + signed.txId.to_bytes(8, "big")
        + signed.txHash
    )
    import ecdsa.util

    state.signature.signature = key.sign(
        message, hashfunc=hashlib.sha256, sigencode=ecdsa.util.sigencode_der
    )

    class Stub:
        def CurrentState(self, request: object) -> Any:  # noqa: ANN401 - gRPC API.
            return state

    client = SimpleNamespace(_stub=Stub(), _vk=key.get_verifying_key())
    found = server_state(client)
    unsigned = server_state(SimpleNamespace(_stub=Stub(), _vk=None))
    client._vk = SigningKey.generate(curve=NIST256p).get_verifying_key()

    assert (found.txId, found.txHash) == (7, b"h" * 32)
    assert unsigned.txId == 7
    with pytest.raises(BadSignatureError):
        server_state(client)


class Lying(MemoryLedger):
    """A ledger whose unverified count says one revision, whatever was written."""

    async def revision_count(self, key: bytes, *, limit: int) -> int:
        """Lie, as a server's unverified history could."""
        return 1


async def test_evidence_written_again_is_caught_by_a_verified_read() -> None:
    """``intact`` rests on the latest revision, read verified, not on a count."""
    ledger = Lying()
    store = LedgerEvidenceStore(
        ledger, namespace="t", max_bytes=4096, subject_key=b"k" * 32
    )
    receipt = await store.record(
        Evidence(kind="kyc.checked", subject="customer:1", facts={"ok": True})
    )
    await ledger.append(receipt.key.encode(), b"rewritten")

    with pytest.raises(EvidenceTampered, match="written again"):
        await store.verify(receipt)
    with pytest.raises(LedgerKeyNotFound):
        await ledger.read(b"missing")


@pytest.mark.parametrize("audit", [False, True])
def test_evidence_without_audit_warns_where_deployed(
    audit: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without audit, the ledger's state ends with the process: ``check`` says so."""
    from domainkit.contrib.evidence import apps as evidence_apps

    monkeypatch.setattr(evidence_apps, "DEPLOYED_ENVIRONMENTS", {"test"})
    installed = ["domainkit.contrib.evidence", ALPHA]
    if audit:
        installed.insert(0, "domainkit.contrib.audit")
    registry.clear()
    try:
        with override_settings(
            INSTALLED_DOMAINS=installed,
            LEDGERS={"evidence": {"URL": "memory://"}},
            EVIDENCE={"SUBJECT_KEY": "s" * 40},
        ):
            setup()
            evidence = registry.get_domain_config("evidence")
            deployed = evidence.deployment_warnings()
            monkeypatch.setattr(evidence_apps, "DEPLOYED_ENVIRONMENTS", set())
            elsewhere = evidence.deployment_warnings()
    finally:
        registry.clear()

    assert deployed == (
        []
        if audit
        else [
            (
                "The evidence ledger's trusted state is kept in memory only, so a "
                "process that starts after the ledger was rolled back trusts it: "
                "install domainkit.contrib.audit, which keeps it in AUDIT.DATABASE."
            )
        ]
    )
    assert elsewhere == []
