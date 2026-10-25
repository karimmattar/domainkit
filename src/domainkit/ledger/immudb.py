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
"""

import asyncio
import logging
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

logger = logging.getLogger(__name__)

DEFAULT_DATABASE: Final = "defaultdb"
SCAN_LIMIT: Final = 1000
"""immudb returns at most this many keys per scan."""
_MISSING: Final = ("not found", "invalid key revision")
"""How immudb 1.11 words a missing key, and a revision past the last one."""
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

    def history(
        self, key: bytes, offset: int, limit: int, sortorder: bool
    ) -> list[Any]:
        """List the revisions of ``key``."""
        ...

    def scan(
        self, key: bytes, prefix: bytes, desc: bool, limit: int
    ) -> dict[bytes, bytes]:
        """List keys with ``prefix``."""
        ...

    def currentState(self) -> Any:  # noqa: ANN401 - SDK API.
        """Fetch (and verify) the server's current state."""
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
    client: Client = sdk
    return client


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


class _Lane:
    """One thread, and the SDK client that lives on it."""

    def __init__(self, config: LedgerConfig, factory: ClientFactory, name: str) -> None:
        """Connect with ``factory`` on first use, on a thread called ``name``."""
        self.config = config
        self.name = name
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
                self._client = self._factory(self.config)
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
    """A verified immudb key-value ledger: a client for work, one for probes."""

    def __init__(
        self, config: LedgerConfig, *, factory: ClientFactory = connect
    ) -> None:
        """Connect lazily with ``factory`` (the real SDK by default)."""
        self.config = config
        self._work = _Lane(config, factory, "domainkit-ledger")
        self._probe = _Lane(config, factory, "domainkit-ledger-check")

    async def append(
        self, key: bytes, value: bytes, *, unchanged_since: int | None = None
    ) -> LedgerWrite:
        """Write a new revision and verify it against the trusted state.

        Raises:
            LedgerChanged: A revision came after ``unchanged_since``.
        """
        if unchanged_since is None:
            response = await self._work.call(
                lambda client: client.verifiedSet(key, value)
            )
        else:
            try:
                response = await self._work.call(
                    lambda client: client.verifiedAppendAfter(
                        key, value, unchanged_since
                    )
                )
            except LedgerKeyExists as exc:
                raise LedgerChanged(
                    "The ledger has a later revision of this key."
                ) from exc
        return LedgerWrite(key, int(response.id))

    async def create(self, key: bytes, value: bytes) -> LedgerWrite:
        """Write the first revision, refused by immudb if the key exists."""
        response = await self._work.call(
            lambda client: client.verifiedCreate(key, value)
        )
        return LedgerWrite(key, int(response.id))

    async def read(self, key: bytes, revision: int | None = None) -> LedgerEntry:
        """Read and verify the latest, or the given, revision."""
        response = await self._work.call(
            lambda client: client.verifiedGet(key, atRevision=revision)
        )
        return LedgerEntry(
            key, bytes(response.value), int(response.id), int(response.revision)
        )

    async def revision_count(self, key: bytes, *, limit: int) -> int:
        """Count revisions from the key's history, up to ``limit``."""
        try:
            history = await self._work.call(
                lambda client: client.history(key, 0, limit, False)
            )
        except LedgerKeyNotFound:
            return 0
        return len(history)

    async def keys(self, prefix: bytes, *, limit: int) -> list[bytes]:
        """Scan keys with ``prefix`` (immudb returns at most 1000)."""
        found = await self._work.call(
            lambda client: client.scan(b"", prefix, False, min(limit, SCAN_LIMIT))
        )
        return list(found)

    async def check(self) -> None:
        """Fetch the current state, which verifies the server's signature.

        The probe has its own client and thread, so it never waits behind
        the ledger's work.
        """
        await self._probe.call(lambda client: client.currentState())

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
