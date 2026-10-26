"""immudb ledgers (``immudb://``): the SDK's verified operations, off the event loop.

immudb-py is synchronous and keeps the trusted state inside the client, so
each ledger runs its client on one dedicated thread: calls never block the
event loop and never race on that state. Readiness checks have a thread and
a client of their own, so a probe never waits behind a slow write. The SDK
only opens plain-text channels; with ``TLS_CA`` this module hands it a TLS
channel that verifies the server certificate. With ``SERVER_SIGNING_KEY``
every state the server returns must carry its signature. See ADR 0011.

A call that outlives ``TIMEOUT`` is given up on, but the SDK cannot be
interrupted, so a write may still land after its caller had an error:
``create``, and ``append`` with ``unchanged_since``, make such a write land
at most once. ADR 0052 says why the client stays the SDK's, on threads.

The state the SDK verifies against is the ledger's, not a client's
(``TrustedRoot``): both clients share it, a reconnect keeps it, and each
advance is saved to a ``trust.StateStore`` (``AUDIT.DATABASE`` with
``domainkit.contrib.audit``), from which a new process starts. A server
whose state is older than it (rolled back) or differs at the same
transaction (forked) is refused, by every call and by the readiness
probe (``trust.LedgerRolledBack``, ``LedgerTampered``). The probe reads
the server's state without trusting it: only verified answers move the
trusted state on. Replicas share the store: the probe starts from a later
state found there, and a save replaces only the state this ledger last
read or saved there; when another replica moved it on, the two states
are proven to lie on one chain first (``verifiedTxById``), so a replica
writing on a rolled-back or forked server is refused instead of saving
the fork over what the others proved (SEC-10b-5). See ADR 0011.
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Final, Protocol
from urllib.parse import unquote, urlsplit

from domainkit.conf import LedgerConfig
from domainkit.conf.global_settings import IMMUDB_PORT
from domainkit.exceptions import ImproperlyConfigured
from domainkit.ledger.base import (
    Ledger,
    LedgerChanged,
    LedgerEntry,
    LedgerError,
    LedgerKeyExists,
    LedgerKeyNotFound,
    LedgerTampered,
    LedgerUnavailable,
    LedgerWrite,
)
from domainkit.ledger.trust import (
    LedgerRolledBack,
    MemoryStateStore,
    StateStore,
    TrustedState,
    registered_states,
)

logger = logging.getLogger(__name__)

DEFAULT_DATABASE: Final = "defaultdb"
SCAN_LIMIT: Final = 1000
"""immudb returns at most this many keys per scan."""
_MISSING: Final = ("not found", "invalid key revision")
"""How immudb 1.11 words a missing key, and a revision past the last one."""
_FORKED: Final = (
    "The ledger's transaction {transaction} is not the one it proved to a "
    "client: it was forked, so do not trust it."
)
_SAVE_TRIES: Final = 3
"""Saves of a state another replica keeps overtaking; then the next call saves."""
_EXISTS: Final = "precondition failed"
"""How immudb 1.11 words a ``create`` of a key that is already written."""


class Client(Protocol):
    """The part of ``immudb.ImmudbClient`` this adapter uses."""

    def login(self, username: str, password: str, database: bytes) -> object:
        """Authenticate and select ``database``."""
        ...

    def verifiedSet(self, key: bytes, value: bytes) -> Any:  # noqa: ANN401 - SDK API.
        """Write ``key`` and verify the write."""
        ...

    def verifiedCreate(self, key: bytes, value: bytes) -> Any:  # noqa: ANN401 - SDK API.
        """Write ``key`` only if it is new, and verify the write (``connect``)."""
        ...

    def verifiedAppendAfter(self, key: bytes, value: bytes, transaction: int) -> Any:  # noqa: ANN401 - SDK API.
        """Write ``key`` only if unwritten since ``transaction`` (``connect``)."""
        ...

    def verifiedGet(self, key: bytes, atRevision: int | None = None) -> Any:  # noqa: ANN401 - SDK API.
        """Read ``key`` and verify the read."""
        ...

    def scan(
        self, key: bytes, prefix: bytes, desc: bool, limit: int
    ) -> dict[bytes, bytes]:
        """List keys with ``prefix``."""
        ...

    def serverState(self) -> Any:  # noqa: ANN401 - SDK types.
        """The server's state, its signature checked, not trusted (``connect``)."""
        ...

    def verifiedTxById(self, tx: int) -> Any:  # noqa: ANN401 - SDK API.
        """Prove transaction ``tx`` against the trusted state; None if it has none."""
        ...

    def shutdown(self) -> None:
        """Close the channel."""
        ...


type ClientFactory = Callable[[LedgerConfig], Client]


@dataclass(frozen=True, slots=True)
class Address:
    """Where an immudb ledger is, and who to log in as."""

    target: str
    user: str
    password: str = field(repr=False)
    """Left out of ``repr``: tracebacks and ``%r`` would show it."""
    database: str


def address_of(config: LedgerConfig) -> Address:
    """Parse ``immudb://user:password@host:port/database``."""
    parts = urlsplit(config.URL.get_secret_value())
    return Address(
        target=f"{parts.hostname}:{parts.port or IMMUDB_PORT}",
        user=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        database=parts.path.lstrip("/") or DEFAULT_DATABASE,
    )


def connect(config: LedgerConfig) -> Client:
    """Open an SDK client: TLS with ``TLS_CA``, signed states with the signing key."""
    try:
        import grpc
        from immudb import ImmudbClient
    except ImportError as exc:
        raise ImproperlyConfigured(
            "immudb ledgers need the immudb extra: pip install 'domainkit[immudb]'."
        ) from exc
    address = address_of(config)
    key = config.SERVER_SIGNING_KEY
    sdk = ImmudbClient(
        address.target,
        publicKeyFile=str(key) if key is not None else None,
        timeout=config.TIMEOUT,
    )
    if config.TLS_CA is not None:
        credentials = grpc.ssl_channel_credentials(
            root_certificates=config.TLS_CA.read_bytes()
        )
        sdk.channel.close()
        sdk.channel = grpc.secure_channel(address.target, credentials)
        # immudb-py 1.5 builds its stub from ``channel``; rebuild it on TLS.
        sdk._resetStub()
    sdk.login(address.user, address.password, database=address.database.encode())
    sdk.verifiedCreate = partial(verified_create, sdk)
    sdk.verifiedAppendAfter = partial(verified_append_after, sdk)
    sdk.serverState = partial(server_state, sdk)
    client: Client = sdk
    return client


def server_state(client: Any) -> Any:  # noqa: ANN401 - SDK types.
    """The server's current state, its signature checked with the signing key.

    Unlike the SDK's ``currentState``, it does not make the state the one
    the client trusts: a state no proof links to the trusted one proves
    nothing.
    """
    from google.protobuf import empty_pb2  # type: ignore[import-untyped]
    from immudb.rootService import State

    state = State.FromGrpc(client._stub.CurrentState(empty_pb2.Empty()))
    if client._vk is not None:
        state.Verify(client._vk)
    return state


def verified_create(client: Any, key: bytes, value: bytes) -> Any:  # noqa: ANN401 - SDK types.
    """The SDK's ``verifiedSet``, refused by immudb if ``key`` is already written."""
    from immudb.grpc import schema_pb2

    must = schema_pb2.Precondition.KeyMustNotExistPrecondition(key=key)
    return _verified_set_if(client, key, value, keyMustNotExist=must)


def verified_append_after(
    client: Any,  # noqa: ANN401 - SDK types.
    key: bytes,
    value: bytes,
    transaction: int,
) -> Any:  # noqa: ANN401 - SDK types.
    """The SDK's ``verifiedSet``, refused if ``key`` changed after ``transaction``.

    Transaction 0 means the caller found the key unwritten: it must still be.
    """
    if transaction == 0:
        return verified_create(client, key, value)
    from immudb.grpc import schema_pb2

    unchanged = schema_pb2.Precondition.KeyNotModifiedAfterTXPrecondition(
        key=key, txID=transaction
    )
    return _verified_set_if(client, key, value, keyNotModifiedAfterTX=unchanged)


def _verified_set_if(
    client: Any,  # noqa: ANN401 - SDK types.
    key: bytes,
    value: bytes,
    **precondition: Any,  # noqa: ANN401 - one immudb precondition.
) -> Any:  # noqa: ANN401 - SDK types.
    """The SDK's verified set, with ``precondition`` added to the request.

    immudb-py 1.5 cannot send preconditions, so this runs its verified set
    through a stub that adds one; the server checks it and writes in one
    step, and the SDK verifies the write as any other.
    """
    from immudb.handler import verifiedSet

    class Conditional:
        """The client's stub, making each verified set conditional."""

        def VerifiableSet(self, request: Any) -> Any:  # noqa: ANN401 - gRPC API.
            request.setRequest.preconditions.add(**precondition)
            return client._stub.VerifiableSet(request)

    return verifiedSet.call(Conditional(), client._rs, key, value, client._vk)


@dataclass(frozen=True, slots=True)
class SdkState:
    """A state as the SDK reads it (``immudb.rootService.State``'s fields)."""

    db: str
    txId: int
    txHash: bytes = field(repr=False)
    publicKey: bytes = field(default=b"", repr=False)
    signature: bytes = field(default=b"", repr=False)


class TrustedRoot:
    """The state a ledger's clients verify against: the SDK's ``RootService``.

    One per ledger, shared by its clients on both threads, so it takes a
    lock. It moves only forward, and only through the SDK's verified
    calls (``set``), or from the store; a client joining (``attach``) or
    the probe (``check``) compares the server's state with it and never
    moves it, except to take the very first state there is (trust on
    first use, when nothing was ever saved).
    """

    def __init__(self) -> None:
        """Trust nothing yet."""
        self._lock = threading.Lock()
        self._state: Any = None
        self._unsaved = False

    def init(self, dbname: str, service: object) -> None:
        """The SDK's login hook: ignored, the state is the ledger's, not the login's."""

    def get(self) -> Any:  # noqa: ANN401 - SDK types.
        """The trusted state (the SDK's ``RootService.get``)."""
        with self._lock:
            return self._state

    def set(self, root: Any) -> None:  # noqa: ANN401 - SDK types.
        """Trust ``root``, which a verified call proved (``RootService.set``).

        One older than the trusted state is not taken: calls on two
        threads may finish out of order.
        """
        with self._lock:
            if self._state is None or root.txId > self._state.txId:
                self._state = root
                self._unsaved = True

    def attach(self, client: Client) -> None:
        """Make ``client`` verify against this state, once the server's passes.

        Raises:
            LedgerRolledBack: The server is older than the trusted state.
            LedgerTampered: It differs from it at the same transaction.
        """
        trusted = self.get()
        state = client.serverState()
        if trusted is None:
            self.set(state)
        else:
            check_state(state, trusted)
        client._rs = self  # type: ignore[attr-defined]

    def adopt(self, stored: TrustedState) -> None:
        """Trust a state the store kept, if it is later than this one's."""
        state = SdkState(
            stored.database,
            stored.transaction,
            stored.digest,
            stored.public_key,
            stored.signature,
        )
        with self._lock:
            if self._state is None or state.txId > self._state.txId:
                self._state = state

    def unsaved(self) -> TrustedState | None:
        """The trusted state, if it moved since last saved; it then counts as saved."""
        with self._lock:
            if not self._unsaved:
                return None
            self._unsaved = False
            state = self._state
        return TrustedState(
            str(state.db),
            int(state.txId),
            bytes(state.txHash),
            bytes(state.signature or b""),
            bytes(state.publicKey or b""),
        )

    def save_failed(self) -> None:
        """Save the state again after the next call: saving it failed."""
        with self._lock:
            self._unsaved = True


def _prove(older: TrustedState, newer: TrustedState, client: Client) -> None:
    """Prove on ``client``'s server that ``newer`` follows ``older`` (its thread only).

    A dual proof from ``older``, with the client trusting it alone for
    the call (``verifiedTxById``), to the server's transaction at
    ``newer``, whose digest must be ``newer``'s.

    Raises:
        LedgerRolledBack: The server has no transaction that late.
        LedgerTampered: Its digest there is another (forked); a proof that
            fails is translated to it too.
    """
    root = TrustedRoot()
    root.adopt(older)
    trusted = client._rs  # type: ignore[attr-defined]
    client._rs = root  # type: ignore[attr-defined]
    try:
        proven = client.verifiedTxById(newer.transaction)
    finally:
        client._rs = trusted  # type: ignore[attr-defined]
    if proven is None:
        raise LedgerRolledBack(
            f"The ledger has no transaction {newer.transaction}, which it proved "
            "to another client: it was rolled back or replaced, so do not trust it."
        )
    if bytes(root.get().txHash) != newer.digest:
        raise LedgerTampered(_FORKED.format(transaction=newer.transaction))


def check_state(state: Any, trusted: Any) -> None:  # noqa: ANN401 - SDK types.
    """Refuse a server ``state`` behind the ``trusted`` one, or beside it.

    Raises:
        LedgerRolledBack: ``state`` is at an earlier transaction.
        LedgerTampered: ``state`` has another digest at the same one.
    """
    if state.txId < trusted.txId:
        raise LedgerRolledBack(
            f"The ledger is at transaction {state.txId}, before transaction "
            f"{trusted.txId}, which it proved to a client: it was rolled back or "
            "replaced, so do not trust it. If it was restored from a backup on "
            "purpose, forget the state kept for it (docs/databases.md#ledgers-immudb)."
        )
    if state.txId == trusted.txId and bytes(state.txHash) != bytes(trusted.txHash):
        raise LedgerTampered(_FORKED.format(transaction=state.txId))


class _Lane:
    """One thread, and the SDK client that lives on it."""

    def __init__(
        self,
        config: LedgerConfig,
        factory: ClientFactory,
        name: str,
        root: TrustedRoot,
    ) -> None:
        """Connect with ``factory`` on first use, on a thread called ``name``.

        Its client verifies against ``root``.
        """
        self.config = config
        self.name = name
        self.root = root
        self._factory = factory
        self._client: Client | None = None
        self._executor: ThreadPoolExecutor | None = None

    async def call[T](self, operation: Callable[[Client], T]) -> T:
        """Run ``operation`` on the thread, within ``TIMEOUT``."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=self.name
            )
        future = asyncio.get_running_loop().run_in_executor(
            self._executor, self._run, operation
        )
        try:
            return await asyncio.wait_for(future, self.config.TIMEOUT)
        except TimeoutError as exc:
            raise LedgerUnavailable(
                f"The ledger did not answer within {self.config.TIMEOUT}s."
            ) from exc

    async def close(self) -> None:
        """Close the client on its thread, waiting at most ``TIMEOUT``; let it go.

        A client busy with a call (one that outlived its caller) is closed
        when the call returns, and its thread then ends; the wait stops at
        ``TIMEOUT``, or sooner if the caller's own budget ends first.
        """
        executor, self._executor = self._executor, None
        if executor is None:
            return
        closed = asyncio.wrap_future(executor.submit(self._disconnect))
        executor.shutdown(wait=False)
        try:
            async with asyncio.timeout(self.config.TIMEOUT):
                await asyncio.shield(closed)
        except TimeoutError:
            logger.warning(
                "The ledger's %s thread is busy; its client closes when the "
                "call returns.",
                self.name,
            )

    def _run[T](self, operation: Callable[[Client], T]) -> T:
        """Call the SDK (this lane's thread only), translating its errors."""
        try:
            if self._client is None:
                client = self._factory(self.config)
                try:
                    self.root.attach(client)
                except BaseException:
                    client.shutdown()
                    raise
                self._client = client
            return operation(self._client)
        except (LedgerError, ImproperlyConfigured):
            raise
        except Exception as exc:
            error = translate(exc)
            if isinstance(error, LedgerUnavailable):
                self._disconnect()
            raise error from exc

    def _disconnect(self) -> None:
        """Drop the client (this lane's thread only); the next call reconnects."""
        client, self._client = self._client, None
        if client is not None:
            client.shutdown()


class ImmudbLedger(Ledger):
    """A verified immudb key-value ledger: a client for work, one for probes.

    Both verify against one ``TrustedRoot``, loaded from ``states`` before
    the first call and saved to it whenever it moves on: by default the
    store an app registered (``trust.use_states``), else this ledger's own
    memory.
    """

    def __init__(
        self,
        config: LedgerConfig,
        *,
        factory: ClientFactory = connect,
        states: StateStore | None = None,
    ) -> None:
        """Connect lazily with ``factory`` (the real SDK by default)."""
        self.config = config
        address = address_of(config)
        self.name = f"{address.target}/{address.database}"
        """What the trusted state is kept under: the server and database."""
        self.root = TrustedRoot()
        self._states = states
        self._memory = MemoryStateStore()
        self._loaded = False
        self._kept: TrustedState | None = None
        """What this ledger last read from or saved to the store."""
        self._work = _Lane(config, factory, "domainkit-ledger", self.root)
        self._probe = _Lane(config, factory, "domainkit-ledger-check", self.root)

    def states(self) -> StateStore:
        """Where the trusted state is kept: the one given, registered, or in memory."""
        return self._states or registered_states() or self._memory

    async def _call[T](self, lane: _Lane, operation: Callable[[Client], T]) -> T:
        """Run ``operation`` on ``lane``, from the kept state; keep where it ends.

        Raises:
            LedgerUnavailable: The kept state could not be read.
        """
        if not self._loaded:
            self._kept = await self._load()
            self._loaded = True
        try:
            return await lane.call(operation)
        finally:
            await self._save(lane)

    async def _load(self) -> TrustedState | None:
        """The state kept in the store; trusted from now on if it is later.

        Raises:
            LedgerUnavailable: The store could not be read.
        """
        try:
            stored = await self.states().load(self.name)
        except Exception as exc:
            raise LedgerUnavailable(
                "The state the ledger was last trusted at could not be read; "
                "nothing was sent to it."
            ) from exc
        if stored is not None:
            self.root.adopt(stored)
        return stored

    async def _save(self, lane: _Lane) -> None:
        """Save the trusted state if it moved; a failure waits for the next call.

        Only over the state this ledger last read or saved: if another
        replica saved one since, both are proven to lie on one chain, the
        later one is trusted, and the save tries again over it.

        Raises:
            LedgerRolledBack: The server no longer has the state another
                replica saved, or this one's.
            LedgerTampered: The two states are not on one chain (forked).
        """
        state = self.root.unsaved()
        if state is None:
            return
        store = self.states()
        try:
            for _ in range(_SAVE_TRIES):
                if await store.save(self.name, state, expected=self._kept):
                    self._kept = state
                    return
                found = await store.load(self.name)
                if found is not None:
                    await self._one_chain(lane, found, state)
                self._kept = found
                if found is not None and found.transaction >= state.transaction:
                    return
            failure = None  # Overtaken each time: the next call saves.
        except LedgerTampered:
            raise
        except Exception as exc:  # noqa: BLE001 - logged below; the call stands.
            failure = exc
        self.root.save_failed()
        logger.warning(
            "Could not save the state ledger %s is trusted at (transaction "
            "%d); a restart until it is saved trusts an older one",
            self.name,
            state.transaction,
            exc_info=failure,
        )

    async def _one_chain(
        self, lane: _Lane, found: TrustedState, state: TrustedState
    ) -> None:
        """Prove ``found`` (another replica's) and ``state`` lie on one chain.

        Raises:
            LedgerRolledBack: The server no longer has the later of them.
            LedgerTampered: They differ where they meet (forked).
        """
        if found.transaction == state.transaction:
            if found.digest != state.digest:
                raise LedgerTampered(_FORKED.format(transaction=found.transaction))
            return
        older, newer = sorted((found, state), key=lambda each: each.transaction)
        try:
            await lane.call(partial(_prove, older, newer))
        except LedgerTampered:
            self.root.adopt(found)  # The later one is not this server's.
            raise
        self.root.adopt(found)

    async def append(
        self, key: bytes, value: bytes, *, unchanged_since: int | None = None
    ) -> LedgerWrite:
        """Write a new revision and verify it against the trusted state.

        Raises:
            LedgerChanged: A revision came after ``unchanged_since``.
        """
        if unchanged_since is None:
            response = await self._write(
                lambda client: client.verifiedSet(key, value)
            )
        else:
            try:
                response = await self._write(
                    lambda client: client.verifiedAppendAfter(
                        key, value, unchanged_since
                    ),
                )
            except LedgerKeyExists as exc:
                raise LedgerChanged(
                    "The ledger has a later revision of this key."
                ) from exc
        return LedgerWrite(key, int(response.id))

    async def create(self, key: bytes, value: bytes) -> LedgerWrite:
        """Write the first revision, refused by immudb if the key exists."""
        response = await self._write(lambda client: client.verifiedCreate(key, value))
        return LedgerWrite(key, int(response.id))

    async def _write[T](self, operation: Callable[[Client], T]) -> T:
        """Run a write; one the server says it has no transaction for is refused.

        A write misses no key, so "not found" means the server lacks the
        transaction the proof starts from: rolled back below it.

        Raises:
            LedgerRolledBack: The server is behind the trusted state.
            LedgerTampered: It differs from it at the same transaction.
        """
        try:
            return await self._call(self._work, operation)
        except LedgerKeyNotFound:
            state = await self._work.call(lambda client: client.serverState())
            check_state(state, self.root.get())
            raise

    async def read(self, key: bytes, revision: int | None = None) -> LedgerEntry:
        """Read and verify the latest, or the given, revision."""
        response = await self._call(
            self._work, lambda client: client.verifiedGet(key, atRevision=revision)
        )
        return LedgerEntry(
            key, bytes(response.value), int(response.id), int(response.revision)
        )

    async def revision_count(self, key: bytes, *, limit: int) -> int:
        """The latest revision's number, read verified, up to ``limit``.

        Not the SDK's ``history``, which nothing verifies: a server could
        answer one revision for a key written twice.
        """
        try:
            latest = await self.read(key)
        except LedgerKeyNotFound:
            return 0
        return min(latest.revision, limit)

    async def keys(self, prefix: bytes, *, limit: int) -> list[bytes]:
        """Scan keys with ``prefix`` (immudb returns at most 1000)."""
        found = await self._call(
            self._work,
            lambda client: client.scan(b"", prefix, False, min(limit, SCAN_LIMIT)),
        )
        return list(found)

    async def check(self) -> None:
        """Fetch the server's state, check its signature, and compare it.

        Refused (``LedgerRolledBack``, ``LedgerTampered``) when it is behind
        the trusted state, or differs from it at the same transaction. The
        probe has its own client and thread, so it never waits behind the
        ledger's work.
        """
        await self._load()  # Another replica may have proven a later one.
        trusted = self.root.get()  # Before the server's: it can only be later.
        state = await self._call(self._probe, lambda client: client.serverState())
        if trusted is not None:
            check_state(state, trusted)

    async def close(self) -> None:
        """Close both clients on their threads, together, within ``TIMEOUT``."""
        await asyncio.gather(self._work.close(), self._probe.close())


def translate(exc: Exception) -> LedgerError:
    """Map an SDK or gRPC error to a ledger error, without leaking credentials."""
    from ecdsa.keys import BadSignatureError
    from immudb.exceptions import ErrCorruptedData, ErrKeyNotFound

    if isinstance(exc, ErrKeyNotFound):
        return LedgerKeyNotFound("The ledger has no such key or revision.")
    if isinstance(exc, ErrCorruptedData | BadSignatureError):
        return LedgerTampered(
            "A ledger proof or server signature did not verify; do not trust it."
        )
    details = getattr(exc, "details", None)
    text = str(details() if callable(details) else exc)
    if any(phrase in text for phrase in _MISSING):
        return LedgerKeyNotFound("The ledger has no such key or revision.")
    if _EXISTS in text:
        return LedgerKeyExists("The ledger already has this key.")
    return LedgerUnavailable(f"The ledger call failed ({type(exc).__name__}).")
