"""Virus scanning of uploads, as they are stored.

::

    UPLOAD_SCANNER = {
        "BACKEND": "domainkit.storage.scanning.ClamAVScanner",
        "OPTIONS": {"host": "clamav", "port": 3310},
    }

``save_upload`` streams each upload through the scanner while the storage
writes it, and the scanner's verdict comes before the last chunk is
handed over: an infected file makes the write fail, so no storage keeps
it (a refusal is 422 ``infected_file``). A scanner that cannot be reached
refuses the upload too (503): an upload is never kept unscanned.

``ClamAVScanner`` speaks clamd's ``INSTREAM`` command over TCP. clamd
speaks no TLS, and asks no one who they are: on another host, every
upload crosses the network in clear. With ``tls=True`` the scanner speaks
TLS to a proxy that ends it in front of clamd (stunnel, say), checks its
certificate (``tls_ca`` for a private CA), and can show one of its own
(``tls_cert``, ``tls_key``) for the proxy to check. ``check`` warns where
deployed (``security.W007``) when the scanner's host is not this one and
TLS is off (ADR 0169).
"""

import asyncio
import ssl
import struct
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar, Final

from domainkit.exceptions import UpstreamError
from domainkit.storage.base import StorageError

__all__ = [
    "ClamAVScanner",
    "Infected",
    "Scanner",
    "ScannerUnavailable",
    "configured_scanner",
]

MAX_REPLY: Final = 4096
"""The longest verdict read from clamd."""


class Infected(StorageError):
    """The scanner found something in the upload; nothing was kept."""

    def __init__(self, signature: str) -> None:
        """Name what was found (logged, never sent to the client)."""
        super().__init__(f"The scanner found {signature}.")
        self.signature = signature


class ScannerUnavailable(UpstreamError):
    """The virus scanner could not scan the upload: 503, nothing kept."""

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "scanner_unavailable"
    detail: ClassVar[str] = "Uploads cannot be checked now; try again later."


class Scanner(ABC):
    """Checks content as it streams past, and refuses it at the end."""

    @abstractmethod
    def scan(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """``chunks``, passed on as they come; ``Infected`` before the last ends.

        Raises:
            Infected: Something was found.
            ScannerUnavailable: The scan could not be made.
        """


class ClamAVScanner(Scanner):
    """clamd's ``INSTREAM``: each chunk sent as it is stored, the verdict at the end."""

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 3310,
        timeout: float = 30.0,
        tls: bool = False,
        tls_ca: str | Path | None = None,
        tls_cert: str | Path | None = None,
        tls_key: str | Path | None = None,
    ) -> None:
        """Talk to clamd at ``host``:``port``; ``timeout`` bounds each step.

        ``tls`` speaks TLS 1.2 or later to a proxy in front of clamd, whose
        certificate must name ``host``, signed by a system CA or by
        ``tls_ca``; ``tls_cert`` and ``tls_key`` are a client certificate
        to show it.

        Raises:
            ValueError: A TLS file without ``tls``, or a certificate without
                its key; or a file TLS cannot read.
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tls = tls
        self.ssl: ssl.SSLContext | None = None
        """The TLS context of the connections; None without ``tls``."""
        files = (tls_ca, tls_cert, tls_key)
        if not tls and any(each is not None for each in files):
            raise ValueError("tls_ca, tls_cert and tls_key need tls=True.")
        if (tls_cert is None) != (tls_key is None):
            raise ValueError("tls_cert and tls_key go together.")
        if tls:
            self.ssl = _client_context(*files)

    async def scan(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Stream ``chunks`` to clamd while passing them on; raise on a finding."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.host,
                    self.port,
                    ssl=self.ssl,
                    server_hostname=self.host if self.ssl else None,
                ),
                self.timeout,
            )
        except (OSError, TimeoutError):  # A refused certificate too (SSLError).
            raise ScannerUnavailable from None
        try:
            writer.write(b"zINSTREAM\0")
            async for chunk in chunks:
                writer.write(struct.pack("!L", len(chunk)) + chunk)
                await self._drain(writer)
                yield chunk
            writer.write(b"\0\0\0\0")
            await self._drain(writer)
            reply = await self._reply(reader)
        finally:
            writer.close()
        verdict = reply.decode(errors="replace").removeprefix("stream: ").strip()
        if verdict.endswith(" FOUND"):
            raise Infected(verdict.removesuffix(" FOUND"))
        if verdict != "OK":
            raise ScannerUnavailable  # "... ERROR": over clamd's size limit, say.

    async def _drain(self, writer: asyncio.StreamWriter) -> None:
        """Wait for clamd to take what was written.

        Raises:
            ScannerUnavailable: It stopped taking it.
        """
        try:
            await asyncio.wait_for(writer.drain(), self.timeout)
        except (OSError, TimeoutError):
            raise ScannerUnavailable from None

    async def _reply(self, reader: asyncio.StreamReader) -> bytes:
        """The verdict clamd sends, up to its NUL.

        Raises:
            ScannerUnavailable: It never came.
        """
        try:
            reply = await asyncio.wait_for(reader.readuntil(b"\0"), self.timeout)
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            raise ScannerUnavailable from None
        return reply[:MAX_REPLY].rstrip(b"\0")


def _client_context(
    ca: str | Path | None, cert: str | Path | None, key: str | Path | None
) -> ssl.SSLContext:
    """A verifying TLS client context: TLS 1.2 or later, ``ca`` trusted.

    Raises:
        ValueError: A file is missing, or TLS cannot read it.
    """
    try:
        context = ssl.create_default_context(cafile=None if ca is None else str(ca))
        if cert is not None and key is not None:
            context.load_cert_chain(str(cert), str(key))
    except OSError as exc:  # A file missing, or one TLS refuses (ssl.SSLError).
        raise ValueError(f"The scanner's TLS files cannot be read: {exc}") from None
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


_made: dict[str, tuple[object, Scanner]] = {}


def configured_scanner() -> Scanner | None:
    """The ``UPLOAD_SCANNER``, made once per configuration; None if unset.

    Raises:
        ImproperlyConfigured: Its ``BACKEND`` is not a ``Scanner``, or its
            ``OPTIONS`` do not fit it.
    """
    from domainkit.conf import settings
    from domainkit.exceptions import ImproperlyConfigured
    from domainkit.utils.module_loading import import_backend

    config = settings.UPLOAD_SCANNER
    if config is None:
        return None
    held = _made.get("scanner")
    if held is not None and held[0] == config:
        return held[1]
    try:
        scanner = import_backend(config.BACKEND)(**config.OPTIONS)
    except (ImportError, TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f"UPLOAD_SCANNER: {exc}") from exc
    if not isinstance(scanner, Scanner):
        raise ImproperlyConfigured(
            f"UPLOAD_SCANNER.BACKEND {config.BACKEND!r} is not a Scanner."
        )
    _made["scanner"] = (config, scanner)
    return scanner
