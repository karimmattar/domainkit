"""``AzureBlobStorage``: files in an Azure Blob Storage container.

::

    STORAGES = {
        "default": {
            "BACKEND": "domainkit.storage.azure.AzureBlobStorage",
            "OPTIONS": {
                "account": "acmefiles",
                "container": "media",
                "account_key": env.str("AZURE_STORAGE_KEY"),
            },
        },
    }

Speaks Blob storage's REST API through ``domainkit.http.client``,
authorized by the account's key (Shared Key), or by an Entra ID token
(``credential``: an application, or the host's managed identity). Files
of up to 8 MiB are one ``Put Blob``; larger ones are staged as blocks and
committed by a block list, so the blob appears whole or not at all
(blocks never committed are dropped by Azure within a week). Reads are
ranged ``Get Blob`` requests.

Presigned URLs are service SASs, which need the account key. A SAS
cannot bind a size or a type: once a client says it uploaded, check the
file's ``size()``.
"""

import base64
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING, Final
from urllib.parse import quote

from domainkit import clock
from domainkit.cloud import CloudAuthError, TokenSource
from domainkit.cloud.azure import blob_sas, shared_key
from domainkit.storage.base import StorageError
from domainkit.storage.remote import ObjectStorage

if TYPE_CHECKING:
    import httpx2

__all__ = ["AzureBlobStorage"]

VERSION: Final = "2023-11-03"
"""The Blob service's API version every request names."""
SCOPE: Final = "https://storage.azure.com/.default"
_ACCOUNT: Final = re.compile(r"[a-z0-9]{3,24}")
_CONTAINER: Final = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){2,62}")


class AzureBlobStorage(ObjectStorage):
    """Files in one container of an Azure Storage account."""

    def __init__(
        self,
        account: str,
        container: str,
        *,
        account_key: str | None = None,
        credential: TokenSource | None = None,
        endpoint: str | None = None,
        prefix: str = "",
        public_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Keep files in ``container`` of the storage ``account``.

        ``account_key`` signs with Shared Key (and presigns); ``credential``
        asks Entra ID for tokens instead. ``endpoint`` replaces
        ``https://<account>.blob.core.windows.net`` (Azurite, a sovereign cloud).

        Raises:
            ValueError: The account or container name is malformed, or
                neither or both of ``account_key`` and ``credential`` is given.
        """
        super().__init__(prefix=prefix, public_url=public_url)
        if not _ACCOUNT.fullmatch(account):
            raise ValueError(f"{account!r} is not a storage account name.")
        if not _CONTAINER.fullmatch(container):
            raise ValueError(f"{container!r} is not a container name.")
        if (account_key is None) == (credential is None):
            raise ValueError("Give account_key or credential, one of them.")
        self.account = account
        self.container = container
        self.endpoint = (endpoint or f"https://{account}.blob.core.windows.net").rstrip(
            "/"
        )
        self._key = account_key
        self.credential = credential
        from domainkit.http.client import HttpClient

        self.client = HttpClient(timeout=timeout, propagate=False)

    def _blob(self, name: str) -> str:
        """The blob's URL."""
        return f"{self.endpoint}/{self.container}/{quote(self.key(name), safe='/-_.~')}"

    def _upload_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """A presigned upload also says it is a block blob."""
        return {**headers, "x-ms-blob-type": "BlockBlob"}

    async def _put(self, name: str, data: bytes) -> None:
        """``Put Blob``."""
        await self._send(
            "PUT",
            self._blob(name),
            headers={"x-ms-blob-type": "BlockBlob"},
            content=data,
            expect={201},
        )

    async def _start(self, name: str) -> str:
        """Blocks need no session: the upload's id is the blob's name."""
        return name

    async def _part(self, name: str, upload: str, number: int, data: bytes) -> str:
        """``Put Block``: staged, not visible until the list is committed."""
        block = base64.b64encode(f"{number:08d}".encode()).decode()
        await self._send(
            "PUT",
            f"{self._blob(name)}?comp=block&blockid={quote(block, safe='')}",
            content=data,
            expect={201},
        )
        return block

    async def _finish(self, name: str, upload: str, parts: Sequence[str]) -> None:
        """``Put Block List``: the blob appears, its blocks in order."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?><BlockList>'
            + "".join(f"<Latest>{block}</Latest>" for block in parts)
            + "</BlockList>"
        )
        await self._send(
            "PUT",
            f"{self._blob(name)}?comp=blocklist",
            headers={"Content-Type": "application/xml"},
            content=body.encode(),
            expect={201},
        )

    async def _abort(self, name: str, upload: str) -> None:
        """Nothing to do: Azure drops blocks never committed."""

    async def _head(self, name: str) -> int | None:
        """``Get Blob Properties``: the size, or None."""
        response = await self._send("HEAD", self._blob(name), expect={200, 404})
        if response.status_code == 404:
            return None
        return int(response.headers["content-length"])

    async def _range(self, name: str, start: int, end: int) -> bytes:
        """A ranged ``Get Blob``."""
        response = await self._send(
            "GET",
            self._blob(name),
            headers={"x-ms-range": f"bytes={start}-{end}"},
            expect={200, 206},
        )
        return response.content

    async def _delete(self, name: str) -> bool:
        """``Delete Blob``: 202 if it was there, 404 if not."""
        response = await self._send("DELETE", self._blob(name), expect={202, 404})
        return response.status_code == 202

    async def _presign(
        self, name: str, method: str, expires: timedelta, headers: dict[str, str]
    ) -> str:
        """A service SAS: read for GET, create (never replace) for PUT.

        Raises:
            CloudAuthError: The storage has no account key to sign with.
        """
        if self._key is None:
            raise CloudAuthError("A SAS needs the storage account's key.")
        query = blob_sas(
            account=self.account,
            container=self.container,
            blob=self.key(name),
            key=self._key,
            permissions="r" if method == "GET" else "c",
            expiry=clock.now().astimezone(UTC) + expires,
        )
        return f"{self._blob(name)}?{query}"

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        expect: set[int],
    ) -> "httpx2.Response":
        """Send an authorized request; its answer, if the status is expected.

        Raises:
            StorageError: Another status (the message names Azure's error code).
        """
        sent = {
            "x-ms-date": format_datetime(clock.now().astimezone(UTC), usegmt=True),
            "x-ms-version": VERSION,
            **(headers or {}),
        }
        if content:
            sent["Content-Length"] = str(len(content))
        if self._key is not None:
            sent["Authorization"] = shared_key(
                method, url, sent, account=self.account, key=self._key
            )
        else:
            assert self.credential is not None  # noqa: S101 - checked when made.
            sent["Authorization"] = f"Bearer {await self.credential.token(SCOPE)}"
        response = await self.client.request(method, url, headers=sent, content=content)
        if response.status_code not in expect:
            code = response.headers.get("x-ms-error-code", "no code")
            raise StorageError(
                f"Azure Blob storage answered {response.status_code} to {method} "
                f"({code})."
            )
        return response
