"""immudb ledgers (``immudb://``): the SDK's verified operations, off the event loop.

immudb-py is synchronous and keeps the trusted state inside the client, so
each ledger runs its client on one dedicated thread: calls never block the
event loop and never race on that state. The SDK only opens plain-text
channels; with ``TLS_CA`` this module hands it a TLS channel that verifies
the server certificate. With ``SERVER_SIGNING_KEY`` every state the server
returns must carry its signature. See ADR 0011.
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any, Final, Protocol
from urllib.parse import unquote, urlsplit

from domainkit.conf import LedgerConfig
from domainkit.conf.global_settings import IMMUDB_PORT
from domainkit.exceptions import ImproperlyConfigured
from domainkit.ledger.base import (
    Ledger,
    LedgerEntry,
    LedgerError,
    LedgerKeyExists,
    LedgerKeyNotFound,
    LedgerTampered,
    LedgerUnavailable,
    LedgerWrite,
)

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
    password: str
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
    client: Client = sdk
    return client


def verified_create(client: Any, key: bytes, value: bytes) -> Any:  # noqa: ANN401 - SDK types.
    """The SDK's ``verifiedSet``, refused by immudb if ``key`` is already written.

    immudb-py 1.5 cannot send preconditions, so this runs its verified set
    through a stub that adds ``keyMustNotExist`` to the request.
    """
    from immudb.grpc import schema_pb2
    from immudb.handler import verifiedSet

    class MustNotExist:
        """The client's stub, making each verified set conditional."""

        def VerifiableSet(self, request: Any) -> Any:  # noqa: ANN401 - gRPC API.
            request.setRequest.preconditions.add(
                keyMustNotExist=schema_pb2.Precondition.KeyMustNotExistPrecondition(
                    key=key
                )
            )
            return client._stub.VerifiableSet(request)

    return verifiedSet.call(MustNotExist(), client._rs, key, value, client._vk)


class ImmudbLedger(Ledger):
    """A verified immudb key-value ledger, one client per instance."""

    def __init__(
        self, config: LedgerConfig, *, factory: ClientFactory = connect
    ) -> None:
        """Connect lazily with ``factory`` (the real SDK by default)."""
        self.config = config
        self._factory = factory
        self._client: Client | None = None
        self._executor: ThreadPoolExecutor | None = None

    async def append(self, key: bytes, value: bytes) -> LedgerWrite:
        """Write a new revision and verify it against the trusted state."""
        response = await self._call(lambda client: client.verifiedSet(key, value))
        return LedgerWrite(key, int(response.id))

    async def create(self, key: bytes, value: bytes) -> LedgerWrite:
        """Write the first revision, refused by immudb if the key exists."""
        response = await self._call(lambda client: client.verifiedCreate(key, value))
        return LedgerWrite(key, int(response.id))

    async def read(self, key: bytes, revision: int | None = None) -> LedgerEntry:
        """Read and verify the latest, or the given, revision."""
        response = await self._call(
            lambda client: client.verifiedGet(key, atRevision=revision)
        )
        return LedgerEntry(
            key, bytes(response.value), int(response.id), int(response.revision)
        )

    async def revision_count(self, key: bytes, *, limit: int) -> int:
        """Count revisions from the key's history, up to ``limit``."""
        try:
            history = await self._call(
                lambda client: client.history(key, 0, limit, False)
            )
        except LedgerKeyNotFound:
            return 0
        return len(history)

    async def keys(self, prefix: bytes, *, limit: int) -> list[bytes]:
        """Scan keys with ``prefix`` (immudb returns at most 1000)."""
        found = await self._call(
            lambda client: client.scan(b"", prefix, False, min(limit, SCAN_LIMIT))
        )
        return list(found)

    async def check(self) -> None:
        """Fetch the current state, which verifies the server's signature."""
        await self._call(lambda client: client.currentState())

    async def close(self) -> None:
        """Close the client on its thread, then stop the thread."""
        executor, self._executor = self._executor, None
        if executor is None:
            return
        await asyncio.get_running_loop().run_in_executor(executor, self._disconnect)
        executor.shutdown(wait=False)

    async def _call[T](self, operation: Callable[[Client], T]) -> T:
        """Run ``operation`` on the ledger thread, within ``TIMEOUT``."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="domainkit-ledger"
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

    def _run[T](self, operation: Callable[[Client], T]) -> T:
        """Call the SDK (ledger thread only), translating its errors."""
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
        """Drop the client (ledger thread only); the next call reconnects."""
        client, self._client = self._client, None
        if client is not None:
            client.shutdown()


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
