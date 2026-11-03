"""``S3Storage``: files in an S3 bucket, or any store that speaks S3's API.

::

    STORAGES = {
        "default": {
            "BACKEND": "domainkit.storage.s3.S3Storage",
            "OPTIONS": {"bucket": "acme-files", "region": "eu-west-1"},
        },
    }

AWS S3, and with ``endpoint`` the stores that speak its API: MinIO
(``path_style=True``), Cloudflare R2 (``region="auto"``), Huawei OBS,
Alibaba OSS, Google Cloud Storage's interoperability API (HMAC keys).
Every request is signed with Signature Version 4 here and sent through
``domainkit.http.client`` (pooled, retried, bounded by the deadline): no
AWS SDK is needed. Credentials come from ``access_key_id`` and
``secret_access_key``, else from the environment as AWS's tools read it
(``domainkit.keys.aws.default_credentials``: variables, a web identity,
the container's).

Files of up to 8 MiB are one ``PUT``; larger ones a multipart upload,
aborted if the content fails. Reads are ranged ``GET`` requests of 4 MiB.
``presigned_url`` and ``presigned_upload`` let clients fetch and send
files without passing through the application; an upload's size and type
are signed, so the URL takes nothing else.
"""

import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Final
from urllib.parse import quote, urlsplit

from domainkit import clock
from domainkit.keys.aws import (
    ALGORITHM,
    AwsCredentials,
    CredentialSource,
    default_credentials,
    signing_key,
)
from domainkit.storage.base import StorageError
from domainkit.storage.remote import ObjectStorage

if TYPE_CHECKING:
    import httpx2

__all__ = ["S3Storage"]

UNSIGNED: Final = "UNSIGNED-PAYLOAD"
_UPLOAD_ID: Final = re.compile(rb"<UploadId>([^<]{1,1024})</UploadId>")
_ERROR_CODE: Final = re.compile(rb"<Code>([A-Za-z0-9]{1,64})</Code>")
_BUCKET: Final = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")


class S3Storage(ObjectStorage):
    """Files in one bucket of S3, or of a store that speaks its API."""

    def __init__(
        self,
        bucket: str,
        *,
        region: str,
        endpoint: str | None = None,
        path_style: bool = False,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        credentials: CredentialSource | None = None,
        prefix: str = "",
        public_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Keep files in ``bucket`` of ``region``.

        ``endpoint`` replaces ``https://s3.<region>.amazonaws.com`` (another
        store, a VPC endpoint); ``path_style`` puts the bucket in the path
        rather than the host name. ``access_key_id`` and
        ``secret_access_key`` are the credentials, or ``credentials`` an
        async callable returning them; without either, the environment's.

        Raises:
            ValueError: The bucket name, the endpoint, or the prefix is malformed,
                or only half of a key pair is given.
        """
        super().__init__(prefix=prefix, public_url=public_url)
        if not _BUCKET.fullmatch(bucket):
            raise ValueError(f"{bucket!r} is not an S3 bucket name.")
        if (access_key_id is None) != (secret_access_key is None):
            raise ValueError(
                "Give both access_key_id and secret_access_key, or neither."
            )
        base = endpoint or f"https://s3.{region}.amazonaws.com"
        parts = urlsplit(base)
        if (
            parts.scheme not in {"https", "http"}
            or not parts.hostname
            or parts.path.strip("/")
        ):
            raise ValueError(
                "An S3 endpoint is a scheme and a host, such as https://minio:9000."
            )
        if path_style:
            self.origin = base.rstrip("/")
            self.base_path = f"/{bucket}"
        else:
            port = f":{parts.port}" if parts.port else ""
            self.origin = f"{parts.scheme}://{bucket}.{parts.hostname}{port}"
            self.base_path = ""
        self.bucket = bucket
        self.region = region
        if access_key_id is not None and secret_access_key is not None:
            static = AwsCredentials(access_key_id, secret_access_key)

            async def fixed() -> AwsCredentials:
                return static

            credentials = fixed
        self.credentials = credentials or default_credentials(region)
        from domainkit.http.client import HttpClient

        self.client = HttpClient(timeout=timeout, propagate=False)

    def path(self, name: str) -> str:
        """The object's path on the origin."""
        return f"{self.base_path}/{quote(self.key(name), safe='/-_.~')}"

    async def _put(self, name: str, data: bytes) -> None:
        """``PUT`` the object."""
        await self._call("PUT", name, body=data, expect={200})

    async def _start(self, name: str) -> str:
        """``POST ?uploads``: the upload's id."""
        response = await self._call("POST", name, params={"uploads": ""}, expect={200})
        found = _UPLOAD_ID.search(response)
        if found is None:
            raise StorageError("S3 began a multipart upload without its id.")
        return found[1].decode()

    async def _part(self, name: str, upload: str, number: int, data: bytes) -> str:
        """``PUT ?partNumber&uploadId``: the part's ETag."""
        params = {"partNumber": str(number), "uploadId": upload}
        _, headers = await self._call_full(
            "PUT", name, params=params, body=data, expect={200}
        )
        etag = headers.get("etag")
        if not etag:
            raise StorageError("S3 took a part without naming its ETag.")
        return etag

    async def _finish(self, name: str, upload: str, parts: Sequence[str]) -> None:
        """``POST ?uploadId`` with the parts, in order."""
        body = (
            "<CompleteMultipartUpload>"
            + "".join(
                f"<Part><PartNumber>{number}</PartNumber><ETag>{_xml(etag)}</ETag></Part>"
                for number, etag in enumerate(parts, start=1)
            )
            + "</CompleteMultipartUpload>"
        )
        response = await self._call(
            "POST", name, params={"uploadId": upload}, body=body.encode(), expect={200}
        )
        # S3 may answer 200 and an error in the body once the parts are joined.
        if b"<Error>" in response:
            raise StorageError(f"S3 refused to join the parts ({_code(response)}).")

    async def _abort(self, name: str, upload: str) -> None:
        """``DELETE ?uploadId``: the parts are dropped."""
        await self._call("DELETE", name, params={"uploadId": upload}, expect={204, 404})

    async def _head(self, name: str) -> int | None:
        """``HEAD``: the size, or None."""
        status, headers = await self._status("HEAD", name)
        if status == 404:
            return None
        if status != 200:
            raise StorageError(f"S3 answered {status} to HEAD.")
        return int(headers["content-length"])

    async def _range(self, name: str, start: int, end: int) -> bytes:
        """A ranged ``GET``."""
        return await self._call(
            "GET", name, headers={"Range": f"bytes={start}-{end}"}, expect={200, 206}
        )

    async def _delete(self, name: str) -> bool:
        """``DELETE``, after a ``HEAD``: S3 answers 204 whether the object was there."""
        if await self._head(name) is None:
            return False
        await self._call("DELETE", name, expect={204, 200})
        return True

    async def _presign(
        self, name: str, method: str, expires: timedelta, headers: dict[str, str]
    ) -> str:
        """A URL with the signature in its query (``X-Amz-Signature``)."""
        credentials = await self.credentials()
        now = clock.now().astimezone(UTC)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        scope = f"{stamp[:8]}/{self.region}/s3/aws4_request"
        signed = {key.lower(): value for key, value in headers.items()}
        signed["host"] = _host(self.origin)
        params = {
            "X-Amz-Algorithm": ALGORITHM,
            "X-Amz-Credential": f"{credentials.access_key_id}/{scope}",
            "X-Amz-Date": stamp,
            "X-Amz-Expires": str(int(expires.total_seconds())),
            "X-Amz-SignedHeaders": ";".join(sorted(signed)),
        }
        if credentials.session_token is not None:
            params["X-Amz-Security-Token"] = credentials.session_token
        path = self.path(name)
        signature = _signature(
            method, path, params, signed, UNSIGNED, credentials, self.region, stamp
        )
        query = _query({**params, "X-Amz-Signature": signature})
        return f"{self.origin}{path}?{query}"

    async def _call(
        self,
        method: str,
        name: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
        expect: set[int],
    ) -> bytes:
        """Send a signed request; its body, if the status is one of ``expect``."""
        content, _ = await self._call_full(
            method, name, params=params, headers=headers, body=body, expect=expect
        )
        return content

    async def _call_full(
        self,
        method: str,
        name: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
        expect: set[int],
    ) -> tuple[bytes, Mapping[str, str]]:
        """Send a signed request; its body and headers, if the status is expected.

        Raises:
            StorageError: Another status; the message names S3's error code.
        """
        response = await self._send(
            method, name, params or {}, dict(headers or {}), body
        )
        if response.status_code not in expect:
            code = _code(response.content)
            raise StorageError(
                f"S3 answered {response.status_code} to {method} ({code})."
            )
        return response.content, response.headers

    async def _status(self, method: str, name: str) -> tuple[int, Mapping[str, str]]:
        """Send a signed request; its status and headers, whatever they are."""
        response = await self._send(method, name, {}, {}, b"")
        return response.status_code, response.headers

    async def _send(
        self,
        method: str,
        name: str,
        params: Mapping[str, str],
        headers: dict[str, str],
        body: bytes,
    ) -> "httpx2.Response":
        """Sign and send one request."""
        credentials = await self.credentials()
        stamp = clock.now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        payload = hashlib.sha256(body).hexdigest()
        headers["x-amz-content-sha256"] = payload
        headers["x-amz-date"] = stamp
        if credentials.session_token is not None:
            headers["x-amz-security-token"] = credentials.session_token
        signed = {
            key.lower(): " ".join(value.split()) for key, value in headers.items()
        }
        signed["host"] = _host(self.origin)
        path = self.path(name)
        signature = _signature(
            method, path, params, signed, payload, credentials, self.region, stamp
        )
        scope = f"{stamp[:8]}/{self.region}/s3/aws4_request"
        headers["Authorization"] = (
            f"{ALGORITHM} Credential={credentials.access_key_id}/{scope}, "
            f"SignedHeaders={';'.join(sorted(signed))}, Signature={signature}"
        )
        query = _query(params)
        url = f"{self.origin}{path}" + (f"?{query}" if query else "")
        return await self.client.request(
            method, url, headers=headers, content=body or None
        )


def _signature(
    method: str,
    path: str,
    params: Mapping[str, str],
    headers: Mapping[str, str],
    payload: str,
    credentials: AwsCredentials,
    region: str,
    stamp: str,
) -> str:
    """SigV4's signature of a request to S3 (its path encoded once, as S3 wants)."""
    names = sorted(headers)
    canonical = "\n".join(
        [
            method,
            path,
            _query(params),
            "".join(f"{name}:{headers[name]}\n" for name in names),
            ";".join(names),
            payload,
        ]
    )
    scope = f"{stamp[:8]}/{region}/s3/aws4_request"
    to_sign = "\n".join(
        [ALGORITHM, stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = signing_key(credentials.secret_access_key, stamp[:8], region, "s3")
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()


def _query(params: Mapping[str, str]) -> str:
    """The query string, each name and value encoded once and sorted (SigV4's)."""
    return "&".join(
        f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}"
        for name, value in sorted(params.items())
    )


def _host(origin: str) -> str:
    """The ``Host`` header of ``origin``."""
    parts = urlsplit(origin)
    default = {"https": 443, "http": 80}[parts.scheme]
    host = parts.hostname or ""
    return host if parts.port in {None, default} else f"{host}:{parts.port}"


def _code(body: bytes) -> str:
    """S3's error code in ``body``, or ``no code``: never the rest, which names keys."""
    found = _ERROR_CODE.search(body)
    return found[1].decode() if found else "no code"


def _xml(text: str) -> str:
    """``text`` escaped for an XML element."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
