"""``GCSStorage``: files in a Google Cloud Storage bucket (``domainkit[cloud]``).

::

    STORAGES = {
        "default": {
            "BACKEND": "domainkit.storage.gcs.GCSStorage",
            "OPTIONS": {
                "bucket": "acme-files",
                "credentials_file": "/run/secrets/gcp.json",
            },
        },
    }

Speaks GCS's JSON API through ``domainkit.http.client``, with a service
account's OAuth token (``domainkit.cloud.google``): from
``credentials_file``, else the metadata server (GKE's workload identity,
Compute Engine). Files of up to 8 MiB are one media upload; larger ones a
resumable upload, its chunks sent as they fill and the session deleted if
the content fails. Reads are ranged ``alt=media`` requests.

Presigned URLs are V4 signed URLs (``GOOG4-RSA-SHA256``), signed by the
key file's key; an upload's size and type are signed with it.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, ClassVar, Final
from urllib.parse import quote, urlsplit

from domainkit import clock
from domainkit.cloud.google import GoogleAccount, MetadataAccount, ServiceAccount
from domainkit.storage.base import StorageError
from domainkit.storage.remote import ObjectStorage

if TYPE_CHECKING:
    import httpx2

__all__ = ["GCSStorage"]

ENDPOINT: Final = "https://storage.googleapis.com"
SCOPE: Final = "https://www.googleapis.com/auth/devstorage.read_write"
ALGORITHM: Final = "GOOG4-RSA-SHA256"
_BUCKET: Final = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")


class GCSStorage(ObjectStorage):
    """Files in one Google Cloud Storage bucket."""

    write_once: ClassVar[Mapping[str, str]] = {"x-goog-if-generation-match": "0"}

    def __init__(
        self,
        bucket: str,
        *,
        credentials_file: str | None = None,
        account: GoogleAccount | None = None,
        endpoint: str = ENDPOINT,
        prefix: str = "",
        public_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Keep files in ``bucket``.

        ``credentials_file`` is a service account's key file; ``account``
        any ``GoogleAccount``; without either, the metadata server's.
        ``endpoint`` replaces Google's (an emulator, in tests).

        Raises:
            ValueError: The bucket name or the prefix is malformed.
            CloudAuthError: The key file cannot be read.
        """
        super().__init__(prefix=prefix, public_url=public_url)
        if not _BUCKET.fullmatch(bucket):
            raise ValueError(f"{bucket!r} is not a Cloud Storage bucket name.")
        self.bucket = bucket
        self.endpoint = endpoint.rstrip("/")
        if account is None:
            account = (
                ServiceAccount.from_file(credentials_file)
                if credentials_file
                else MetadataAccount()
            )
        self.account = account
        self._offsets: dict[str, int] = {}
        from domainkit.http.client import HttpClient

        self.client = HttpClient(timeout=timeout, propagate=False)

    def _object(self, name: str) -> str:
        """The JSON API's URL of the object."""
        key = quote(self.key(name), safe="")
        return f"{self.endpoint}/storage/v1/b/{self.bucket}/o/{key}"

    def _uploads(self) -> str:
        """The JSON API's upload URL of the bucket."""
        return f"{self.endpoint}/upload/storage/v1/b/{self.bucket}/o"

    async def _put(self, name: str, data: bytes) -> None:
        """A media upload: one request."""
        await self._send(
            "POST",
            self._uploads(),
            params={"uploadType": "media", "name": self.key(name)},
            content=data,
            expect={200},
        )

    async def _start(self, name: str) -> str:
        """Open a resumable session; its URL is the upload's id."""
        response = await self._send(
            "POST",
            self._uploads(),
            params={"uploadType": "resumable", "name": self.key(name)},
            expect={200},
        )
        session = response.headers.get("location")
        if not session:
            raise StorageError("Cloud Storage opened an upload without its URL.")
        self._offsets[session] = 0
        return session

    async def _part(self, name: str, upload: str, number: int, data: bytes) -> str:
        """Send the next chunk; Google answers 308 until the upload is complete."""
        start = self._offsets[upload]
        end = start + len(data) - 1
        await self._send(
            "PUT",
            upload,
            headers={"Content-Range": f"bytes {start}-{end}/*"},
            content=data,
            expect={308, 200},
        )
        self._offsets[upload] = end + 1
        return str(number)

    async def _finish(self, name: str, upload: str, parts: Sequence[str]) -> None:
        """Say the total: the object appears."""
        total = self._offsets.pop(upload)
        await self._send(
            "PUT",
            upload,
            headers={"Content-Range": f"bytes */{total}"},
            expect={200, 201},
        )

    async def _abort(self, name: str, upload: str) -> None:
        """Delete the session (Google answers 499 to say it is cancelled)."""
        self._offsets.pop(upload, None)
        await self._send("DELETE", upload, expect={499, 204, 404, 410})

    async def _head(self, name: str) -> int | None:
        """The object's metadata: its size, or None."""
        response = await self._send("GET", self._object(name), expect={200, 404})
        if response.status_code == 404:
            return None
        try:
            return int(response.json()["size"])
        except (ValueError, KeyError, TypeError):
            raise StorageError("Cloud Storage answered without the size.") from None

    async def _range(self, name: str, start: int, end: int) -> bytes:
        """A ranged media download."""
        response = await self._send(
            "GET",
            self._object(name),
            params={"alt": "media"},
            headers={"Range": f"bytes={start}-{end}"},
            expect={200, 206},
        )
        return response.content

    async def _delete(self, name: str) -> bool:
        """``DELETE``: 204 if it was there, 404 if not."""
        response = await self._send("DELETE", self._object(name), expect={204, 404})
        return response.status_code == 204

    async def _presign(
        self, name: str, method: str, expires: timedelta, headers: dict[str, str]
    ) -> str:
        """A V4 signed URL, signed by the service account's key."""
        now = clock.now().astimezone(UTC)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        scope = f"{stamp[:8]}/auto/storage/goog4_request"
        signed = {key.lower(): value for key, value in headers.items()}
        signed["host"] = urlsplit(self.endpoint).netloc
        names = ";".join(sorted(signed))
        params = {
            "X-Goog-Algorithm": ALGORITHM,
            "X-Goog-Credential": f"{self.account.email}/{scope}",
            "X-Goog-Date": stamp,
            "X-Goog-Expires": str(int(expires.total_seconds())),
            "X-Goog-SignedHeaders": names,
        }
        path = f"/{self.bucket}/{quote(self.key(name), safe='/-_.~')}"
        canonical = "\n".join(
            [
                method,
                path,
                _query(params),
                "".join(f"{key}:{signed[key]}\n" for key in sorted(signed)),
                names,
                "UNSIGNED-PAYLOAD",
            ]
        )
        to_sign = "\n".join(
            [ALGORITHM, stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()]
        )
        signature = self.account.sign(to_sign.encode()).hex()
        query = _query({**params, "X-Goog-Signature": signature})
        return f"{self.endpoint}{path}?{query}"

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        expect: set[int],
    ) -> "httpx2.Response":
        """Send an authorized request; its answer, if the status is expected.

        Raises:
            StorageError: Another status (the message names it, not the body).
        """
        token = await self.account.token(SCOPE)
        sent = {"Authorization": f"Bearer {token}", **(headers or {})}
        response = await self.client.request(
            method, url, params=params, headers=sent, content=content
        )
        if response.status_code not in expect:
            raise StorageError(
                f"Cloud Storage answered {response.status_code} to {method}."
            )
        return response


def _query(params: Mapping[str, str]) -> str:
    """The query string, each name and value encoded once and sorted."""
    return "&".join(
        f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}"
        for name, value in sorted(params.items())
    )
