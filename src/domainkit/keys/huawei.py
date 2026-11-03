"""Huawei Cloud KMS (Data Encryption Workshop) as a ``KeyProvider``.

::

    provider = HuaweiKmsKeyProvider(
        "0d0466b0-...", region="ap-southeast-3", project_id="...",
        access_key=env.str("HUAWEI_AK"), secret_key=env.str("HUAWEI_SK"),
    )

Data keys come from ``create-datakey`` (256 bits) under the customer
master key, are unwrapped by ``decrypt-datakey``, whose digest of the
key is checked, and wrapped again by ``encrypt-datakey``, under the
master key's current material. Calls are signed with the access key
(``SDK-HMAC-SHA256``, Huawei's API Gateway signature). A wrapped key is
``huawei-kms:`` and KMS's ciphertext, in hex.
"""

import hashlib
import hmac
import re
from collections.abc import Mapping
from datetime import UTC
from typing import Any, Final
from urllib.parse import quote, urlsplit

from domainkit import clock

from . import DATA_KEY_BYTES, DataKey, KeyProvider, KeyProviderError, UnknownKey
from ._http import call_json, field

__all__ = ["HuaweiKmsKeyProvider", "sign"]

NAME: Final = "huawei-kms"
ALGORITHM: Final = "SDK-HMAC-SHA256"
TITLE: Final = "Huawei KMS"
_HEX: Final = re.compile(r"(?:[0-9a-fA-F]{2})+")


def sign(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    access_key: str,
    secret_key: str,
) -> dict[str, str]:
    """``headers`` with ``X-Sdk-Date`` and an ``SDK-HMAC-SHA256`` signature.

    Every header given is signed, with ``Host`` from ``url``; the
    canonical path ends with ``/``, as the gateway computes it.
    """
    stamp = clock.now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    parts = urlsplit(url)
    signed = {key.lower(): value.strip() for key, value in headers.items()}
    signed["host"] = parts.netloc
    signed["x-sdk-date"] = stamp
    names = ";".join(sorted(signed))
    path = quote(parts.path or "/", safe="/-_.~")
    canonical = "\n".join(
        [
            method.upper(),
            path if path.endswith("/") else f"{path}/",
            "&".join(sorted(parts.query.split("&"))) if parts.query else "",
            "".join(f"{name}:{signed[name]}\n" for name in sorted(signed)),
            names,
            hashlib.sha256(body).hexdigest(),
        ]
    )
    to_sign = f"{ALGORITHM}\n{stamp}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    signature = hmac.new(
        secret_key.encode(), to_sign.encode(), hashlib.sha256
    ).hexdigest()
    return {
        **headers,
        "X-Sdk-Date": stamp,
        "Authorization": (
            f"{ALGORITHM} Access={access_key}, SignedHeaders={names}, "
            f"Signature={signature}"
        ),
    }


class HuaweiKmsKeyProvider(KeyProvider):
    """Data keys under one Huawei KMS master key (see the module)."""

    name = NAME

    def __init__(
        self,
        key_id: str,
        *,
        region: str,
        project_id: str,
        access_key: str,
        secret_key: str,
        endpoint: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Use the master key ``key_id`` of ``project_id`` in ``region``."""
        from domainkit.http.client import HttpClient

        if not (key_id and project_id and access_key and secret_key):
            raise ValueError("Huawei KMS needs a key id, a project, and an access key.")
        self.key_id = key_id
        self.region = region
        self.project_id = project_id
        self._access_key = access_key
        self._secret_key = secret_key
        self.endpoint = (endpoint or f"https://kms.{region}.myhuaweicloud.com").rstrip(
            "/"
        )
        self.client = HttpClient(timeout=timeout, propagate=False)

    def __repr__(self) -> str:
        """The key and region; no credential."""
        return f"HuaweiKmsKeyProvider(key_id={self.key_id!r}, region={self.region!r})"

    async def generate_data_key(self) -> DataKey:
        """``create-datakey``: 32 bytes, and them under the master key."""
        answer = await self._call(
            "create-datakey", {"key_id": self.key_id, "datakey_length": "256"}
        )
        plaintext = _hex(field(answer, "plain_text", TITLE))[:DATA_KEY_BYTES]
        if len(plaintext) != DATA_KEY_BYTES:
            raise KeyProviderError("Huawei KMS answered a key of another size.")
        return DataKey(plaintext, f"{NAME}:{_cipher(answer)}")

    async def unwrap(self, wrapped: str) -> bytes:
        """``decrypt-datakey``, its digest of the key checked."""
        cipher = wrapped.removeprefix(f"{NAME}:")
        if not self.owns(wrapped) or not _HEX.fullmatch(cipher):
            raise UnknownKey("Huawei KMS did not wrap that key.")
        answer = await self._call(
            "decrypt-datakey",
            {
                "key_id": self.key_id,
                "cipher_text": cipher,
                "datakey_cipher_length": str(DATA_KEY_BYTES),
            },
            unwrap=True,
        )
        plaintext = _hex(field(answer, "data_key", TITLE))
        digest = answer.get("datakey_dgst")
        if (
            len(plaintext) != DATA_KEY_BYTES
            or not isinstance(digest, str)
            or not hmac.compare_digest(
                digest.lower(), hashlib.sha256(plaintext).hexdigest()
            )
        ):
            raise KeyProviderError(
                "Huawei KMS answered a key its digest does not match."
            )
        return plaintext

    async def rewrap(self, wrapped: str) -> str:
        """``encrypt-datakey`` of the key ``wrapped`` holds, under current material."""
        plaintext = await self.unwrap(wrapped)
        answer = await self._call(
            "encrypt-datakey",
            {
                "key_id": self.key_id,
                "plain_text": (plaintext + hashlib.sha256(plaintext).digest()).hex(),
                "datakey_plain_length": str(DATA_KEY_BYTES),
            },
        )
        return f"{NAME}:{_cipher(answer)}"

    async def _call(
        self, action: str, payload: Mapping[str, Any], *, unwrap: bool = False
    ) -> dict[str, Any]:
        """``POST /v1.0/<project>/kms/<action>``, signed."""
        import json

        url = f"{self.endpoint}/v1.0/{self.project_id}/kms/{action}"
        body = json.dumps(dict(payload)).encode()
        headers = sign(
            "POST",
            url,
            {"Content-Type": "application/json", "X-Project-Id": self.project_id},
            body,
            access_key=self._access_key,
            secret_key=self._secret_key,
        )
        return await call_json(
            self.client,
            TITLE,
            action,
            url,
            content=body,
            headers=headers,
            unwrap=unwrap,
        )


def _hex(value: str) -> bytes:
    """Bytes from hex.

    Raises:
        KeyProviderError: It is not hex.
    """
    try:
        return bytes.fromhex(value)
    except ValueError:
        raise KeyProviderError("Huawei KMS answered a key that is not hex.") from None


def _cipher(answer: Mapping[str, Any]) -> str:
    """The ciphertext of an answer, if hex.

    Raises:
        KeyProviderError: It is missing, or not hex.
    """
    cipher = field(answer, "cipher_text", TITLE)
    if not _HEX.fullmatch(cipher):
        raise KeyProviderError("Huawei KMS answered a ciphertext that is not hex.")
    return cipher
