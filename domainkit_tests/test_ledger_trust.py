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

    def verifiedTxById(self, tx: int) -> Any:  # noqa: ANN401 - SDK API.
        """Prove transaction ``tx`` and the trusted state lie on one chain.

        None when the server has no transaction ``tx``, as the SDK answers.
        A later one is trusted then, with the server's digest there.
        """
        if tx > self.server.tx:
            return None
        trusted = self._rs.get()
        self.server.prove(trusted)
        if tx > trusted.txId:
            self._rs.set(
                SimpleNamespace(
                    db="evidence",
                    txId=tx,
                    txHash=self.server.digests[tx],
                    publicKey=b"",
                    signature=b"",
                )
            )
        return [self.server.entries[tx - 1][0]]

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

    with pytest.raises(LedgerRolledBack) as refused:
        await ledger.read(b"k")
    with pytest.raises(LedgerRolledBack):
        await ledger.check()
    await ledger.close()

    assert str(refused.value) == (
        "The ledger is at transaction 1, before transaction 3, which it proved to "
        "a client: it was rolled back or replaced, so do not trust it. If it was "
        "restored from a backup on purpose, forget the state kept for it "
        "(docs/databases.md#ledgers-immudb)."
    )


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

    with pytest.raises(LedgerTampered) as probed:
        await ledger.check()
    with pytest.raises(LedgerTampered, match="did not verify"):
        await ledger.read(b"k")
    await ledger.close()

    assert not isinstance(probed.value, LedgerRolledBack)
    assert str(probed.value) == (
        "The ledger's transaction 2 is not the one it proved to a client: it was "
        "forked, so do not trust it."
    )


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

    async def save(
        self, ledger: str, state: TrustedState, *, expected: TrustedState | None
    ) -> bool:
        """Fail while failures are left."""
        if self.failures:
            self.failures -= 1
            raise OSError("the database is down")
        return await super().save(ledger, state, expected=expected)


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
    root.set(SdkState("db", 5, b"5" * 32))  # The same state again: nothing new.
    root.adopt(TrustedState("db", 5, b"5" * 32))

    assert root.unsaved() is None
    root.adopt(TrustedState("db", 9, b"9" * 32, b"sig", b"key"))
    assert root.get() == SdkState("db", 9, b"9" * 32, b"key", b"sig")
    assert root.unsaved() is None  # Taken from the store: nothing to save.
    root.save_failed()
    assert root.unsaved() == TrustedState("db", 9, b"9" * 32, b"sig", b"key")


async def test_memory_stores_compare_and_set() -> None:
    """A save replaces only the state the saver expects (SEC-10b-5)."""
    store = MemoryStateStore()
    five = TrustedState("db", 5, b"5" * 32)
    later = TrustedState("db", 7, b"7" * 32)

    assert await store.save("l", five, expected=None)
    assert not await store.save("l", TrustedState("db", 3, b"3" * 32), expected=None)
    assert not await store.save("l", later, expected=TrustedState("db", 5, b"x" * 32))
    assert await store.load("l") == five
    assert await store.save("l", later, expected=five)
    assert await store.load("l") == later
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
    assert not await registered.save(
        NAME, TrustedState("evidence", 1, b"1" * 32), expected=None
    )
    server.roll_back(1)

    with pytest.raises(LedgerRolledBack):
        await ledger_of(server).check()
    kept = await registered.load(NAME)
    signed = TrustedState("evidence", 9, b"9" * 32, b"signature", b"public key")
    assert await registered.save("signed", signed, expected=None)
    forked = TrustedState("evidence", 9, b"x" * 32)
    assert not await registered.save("signed", forked, expected=forked)
    assert await registered.load("signed") == signed
    later = TrustedState("evidence", 12, b"c" * 32)
    assert await registered.save("later", later, expected=None)
    assert await registered.save("later", signed, expected=later)

    assert kept is not None
    assert (kept.database, kept.transaction, len(kept.digest)) == ("evidence", 2, 32)
    assert await registered.load("another") is None
    assert await registered.load("signed") == signed
    assert await registered.load("later") == signed


async def test_a_rollback_of_another_replicas_writes_is_refused() -> None:
    """SEC-10b-5: replicas share the store; a rollback below one's writes is refused.

    B proves transactions 2 to 5 and saves them; the server is rolled back
    to 1. A, which last read the store at 1, must not trust the server on
    its own word: its probe reads the store again, its write is refused,
    and the store keeps 5, so a replica that starts later refuses too. In
    the PoC, A's probe and writes passed, and its saves put the fork over 5.
    """
    server = FakeServer()
    store = MemoryStateStore()
    a = ledger_of(server, store)
    b = ledger_of(server, store)
    await a.append(b"evidence/a", b"1")
    for n in range(4):
        await b.append(b"evidence/b", str(n).encode())
    await b.close()
    server.roll_back(1)

    with pytest.raises(LedgerRolledBack):
        await a.check()
    with pytest.raises(LedgerRolledBack):
        await a.append(b"evidence/a", b"after")
    kept = await store.load(NAME)
    c = ledger_of(server, store)
    with pytest.raises(LedgerRolledBack):
        await c.check()
    await a.close()
    await c.close()

    assert kept is not None
    assert kept.transaction == 5


async def test_a_write_on_a_rolled_back_server_is_refused_when_saved() -> None:
    """Without a probe first, the save finds 5, which the server no longer has."""
    server = FakeServer()
    store = MemoryStateStore()
    a = ledger_of(server, store)
    b = ledger_of(server, store)
    await a.append(b"k", b"1")
    for n in range(4):
        await b.append(b"k", str(n).encode())
    server.roll_back(1)

    with pytest.raises(LedgerRolledBack, match="no transaction 5"):
        await a.append(b"k", b"after")
    kept = await store.load(NAME)
    await a.close()
    await b.close()

    assert kept is not None
    assert kept.transaction == 5
    assert a.root.get().txId == 5  # Every later call proves from it.


@pytest.mark.parametrize("where", ["after", "at"])
async def test_a_write_on_a_forked_server_is_refused_when_saved(where: str) -> None:
    """B saved 3; then the server forked: A's write there does not follow 3.

    A writes transaction 4 after a forged 3, or a 3 of its own in place
    of B's: both are refused as forks, and the store keeps B's 3.
    """
    server = FakeServer()
    store = MemoryStateStore()
    a = ledger_of(server, store)
    b = ledger_of(server, store)
    await a.append(b"k", b"1")
    await b.append(b"k", b"2")
    await b.append(b"k", b"3")
    if where == "after":
        server.fork(at=3)
    else:
        server.roll_back(2)

    with pytest.raises(LedgerTampered) as refused:
        await a.append(b"k", b"forked")
    kept = await store.load(NAME)
    await a.close()
    await b.close()

    assert not isinstance(refused.value, LedgerRolledBack)
    assert kept is not None
    assert kept.transaction == 3


async def test_replicas_writing_in_turn_keep_the_latest_state() -> None:
    """On a sound server, a save overtaken by another replica proves, then saves."""
    server = FakeServer()
    store = MemoryStateStore()
    a = ledger_of(server, store)
    b = ledger_of(server, store)
    for n in range(3):
        await a.append(b"k", str(n).encode())
        await b.append(b"k", str(n).encode())
    await a.read(b"k")  # Proves to 6, which B saved: the same state.
    await b.close()
    await a.append(b"k", b"last")
    kept = await store.load(NAME)
    await a.close()

    assert kept is not None
    assert kept.transaction == server.tx == 7


class Overtaken(MemoryStateStore):
    """A store another replica always saves to first."""

    async def save(
        self, ledger: str, state: TrustedState, *, expected: TrustedState | None
    ) -> bool:
        """Never: someone else was first."""
        return False


async def test_a_save_overtaken_every_time_waits_for_the_next_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After a few tries the state stays unsaved, logged, for the next call."""
    server = FakeServer()
    ledger = ledger_of(server, Overtaken())

    with caplog.at_level(logging.WARNING, "domainkit.ledger.immudb"):
        await ledger.append(b"k", b"1")
    await ledger.close()

    assert "Could not save the state ledger" in caplog.text
    assert ledger.root.unsaved() is not None


class Unreachable(FakeClient):
    """A client whose proofs between states fail on the network."""

    def verifiedTxById(self, tx: int) -> Any:  # noqa: ANN401 - SDK API.
        """Fail as a dropped connection does."""
        raise RpcError("connection reset")


async def test_a_proof_that_cannot_be_made_waits_for_the_next_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The write landed: a failed proof leaves the state unsaved, logged."""
    server = FakeServer()
    store = MemoryStateStore()
    b = ledger_of(server, store)
    a = ImmudbLedger(config(), factory=lambda _: Unreachable(server), states=store)
    await a.append(b"k", b"1")
    await b.append(b"k", b"2")

    with caplog.at_level(logging.WARNING, "domainkit.ledger.immudb"):
        written = await a.append(b"k", b"3")
    await a.close()
    await b.close()

    assert written.transaction == 3
    assert (await store.load(NAME)).transaction == 2  # type: ignore[union-attr]
    assert "Could not save the state ledger" in caplog.text


class Forking(FakeClient):
    """A client of a server that forks right after answering a write, when armed."""

    armed = False

    def verifiedSet(self, key: bytes, value: bytes) -> Any:  # noqa: ANN401 - SDK API.
        """Write, proven; then the server rewrites that transaction."""
        response = super().verifiedSet(key, value)
        if Forking.armed:
            self.server.fork(at=self.server.tx)
        return response


async def test_a_write_the_server_rewrote_since_is_refused_when_saved() -> None:
    """Proving B's state to A's lands on another digest than A's: forked."""
    server = FakeServer()
    store = MemoryStateStore()
    a = ImmudbLedger(config(), factory=lambda _: Forking(server), states=store)
    b = ledger_of(server, store)
    await a.append(b"k", b"1")
    await b.append(b"k", b"2")
    Forking.armed = True
    try:
        with pytest.raises(LedgerTampered, match="transaction 3 is not the one"):
            await a.append(b"k", b"3")
    finally:
        Forking.armed = False
    await a.close()
    await b.close()

    assert (await store.load(NAME)).transaction == 2  # type: ignore[union-attr]


class Missing(FakeClient):
    """A client whose writes the server answers "not found" while it is sound."""

    def verifiedSet(self, key: bytes, value: bytes) -> Any:  # noqa: ANN401 - SDK API.
        """Answer as a missing key does."""
        raise RpcError("key not found")


async def test_a_write_refused_by_a_sound_server_keeps_its_error() -> None:
    """Only a server behind the trusted state turns "not found" into a rollback."""
    server = FakeServer()
    ledger = ImmudbLedger(config(), factory=lambda _: Missing(server))

    with pytest.raises(LedgerKeyNotFound):
        await ledger.append(b"k", b"1")
    await ledger.close()


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
            assert type(request).__name__ == "Empty"
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

    def __init__(self) -> None:
        """Count the reads."""
        super().__init__()
        self.reads: list[int | None] = []

    async def read(self, key: bytes, revision: int | None = None) -> LedgerEntry:
        """Read, counted."""
        self.reads.append(revision)
        return await super().read(key, revision)

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
    intact = await store.verify(receipt)
    once = list(ledger.reads)
    await ledger.append(receipt.key.encode(), b"rewritten")
    ledger.reads.clear()

    with pytest.raises(EvidenceTampered, match="written again"):
        await store.verify(receipt)
    assert intact.intact
    assert once == [None]  # Written once: one read, of the latest.
    assert ledger.reads == [None, 1]
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
