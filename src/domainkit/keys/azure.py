"""Azure Key Vault (or Managed HSM) as a ``KeyProvider``.

::

    provider = AzureKeyVaultKeyProvider("https://acme.vault.azure.net", "fields")

A data key is 32 random bytes made here, wrapped by the key's
``wrapkey`` (``RSA-OAEP-256`` by default; ``A256KW`` for a Managed HSM's
AES key) under its current version, which is read once every five
minutes, and unwrapped by ``unwrapkey`` under the version that wrapped
it. ``rewrap`` unwraps and wraps again, under the version now current.
The caller is an Entra application or the managed identity, with the
``wrapKey`` and ``unwrapKey`` permissions (``Key Vault Crypto User``). A
wrapped key is ``azure-kv:``, the key version, and Key Vault's value.
"""

import base64
import re
import secrets
import time
from typing import Any, Final
from urllib.parse import quote

from domainkit.cloud import TokenSource

from . import (
    DATA_KEY_BYTES,
    DataKey,
    KeyProvider,
    KeyProviderError,
    UnknownKey,
    _unb64,
)
from ._http import call_json, field

__all__ = ["AzureKeyVaultKeyProvider"]

NAME: Final = "azure-kv"
SCOPE: Final = "https://vault.azure.net/.default"
API_VERSION: Final = "7.4"
TITLE: Final = "Azure Key Vault"
VERSION_TTL: Final = 300.0
"""Seconds the key's current version is kept before it is read again."""
_VERSION: Final = re.compile(r"[0-9A-Za-z]{1,64}")


class AzureKeyVaultKeyProvider(KeyProvider):
    """Data keys under one Key Vault key (see the module)."""

    name = NAME

    def __init__(
        self,
        vault_url: str,
        key_name: str,
        *,
        credential: TokenSource | None = None,
        algorithm: str = "RSA-OAEP-256",
        timeout: float | None = None,
    ) -> None:
        """Use the key ``key_name`` of ``vault_url``, called as ``credential``.

        ``credential`` defaults to the managed identity.

        Raises:
            ValueError: ``key_name`` is empty, or the algorithm unknown.
        """
        from domainkit.cloud.azure import ManagedIdentity
        from domainkit.http.client import HttpClient

        if not key_name or algorithm not in {"RSA-OAEP-256", "RSA-OAEP", "A256KW"}:
            raise ValueError(
                "A Key Vault key needs a name, and RSA-OAEP(-256) or A256KW."
            )
        self.vault_url = vault_url.rstrip("/")
        self.key_name = key_name
        self.algorithm = algorithm
        self.credential = credential or ManagedIdentity()
        self.client = HttpClient(
            base_url=self.vault_url, timeout=timeout, propagate=False
        )
        self._current: tuple[str, float] | None = None

    def __repr__(self) -> str:
        """The vault and key; no credential."""
        return (
            f"AzureKeyVaultKeyProvider(vault_url={self.vault_url!r}, "
            f"key_name={self.key_name!r})"
        )

    async def generate_data_key(self) -> DataKey:
        """32 random bytes, and them wrapped under the key's current version."""
        plaintext = secrets.token_bytes(DATA_KEY_BYTES)
        return DataKey(plaintext, await self._wrap(plaintext))

    async def unwrap(self, wrapped: str) -> bytes:
        """``unwrapkey``, under the version that wrapped it."""
        version, _, value = wrapped.removeprefix(f"{NAME}:").partition(":")
        if not self.owns(wrapped) or not _VERSION.fullmatch(version) or not value:
            raise UnknownKey("Azure Key Vault did not wrap that key.")
        answer = await self._call(
            f"{version}/unwrapkey", {"alg": self.algorithm, "value": value}, unwrap=True
        )
        try:
            plaintext = _unb64(field(answer, "value", TITLE))
        except ValueError:
            plaintext = b""
        if len(plaintext) != DATA_KEY_BYTES:
            raise KeyProviderError("Azure Key Vault answered without the key.")
        return plaintext

    async def rewrap(self, wrapped: str) -> str:
        """Unwrapped, then wrapped under the version now current."""
        return await self._wrap(await self.unwrap(wrapped))

    async def _wrap(self, plaintext: bytes) -> str:
        """``wrapkey`` under the current version: ``azure-kv:<version>:<value>``."""
        version = await self._version()
        value = base64.urlsafe_b64encode(plaintext).rstrip(b"=").decode()
        answer = await self._call(
            f"{version}/wrapkey", {"alg": self.algorithm, "value": value}
        )
        return f"{NAME}:{version}:{field(answer, 'value', TITLE)}"

    async def _version(self) -> str:
        """The key's current version, read again every five minutes."""
        now = time.monotonic()
        if self._current is not None and self._current[1] > now:
            return self._current[0]
        response = await self._get()
        kid = field(response.get("key") or {}, "kid", TITLE)
        version = kid.rstrip("/").rpartition("/")[2]
        if not _VERSION.fullmatch(version):
            raise KeyProviderError("Azure Key Vault answered a key without a version.")
        self._current = (version, now + VERSION_TTL)
        return version

    async def _get(self) -> dict[str, Any]:
        """``GET /keys/<name>``: the key's current version."""
        import httpx2

        headers = await self._headers("read")
        try:
            response = await self.client.get(
                f"/keys/{quote(self.key_name, safe='')}",
                params={"api-version": API_VERSION},
                headers=headers,
            )
        except httpx2.HTTPError as exc:
            raise KeyProviderError(
                "Azure Key Vault could not be reached (read)."
            ) from exc
        if response.status_code != 200:
            raise KeyProviderError(
                f"Azure Key Vault refused read ({response.status_code})."
            )
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise KeyProviderError("Azure Key Vault answered read without JSON.")
        return answer

    async def _headers(self, action: str) -> dict[str, str]:
        """The credential's token.

        Raises:
            KeyProviderError: Entra refused one.
        """
        from domainkit.cloud import CloudAuthError

        try:
            return {"Authorization": f"Bearer {await self.credential.token(SCOPE)}"}
        except CloudAuthError as exc:
            raise KeyProviderError(
                f"Azure Key Vault's token was refused ({action})."
            ) from exc

    async def _call(
        self, action: str, payload: dict[str, Any], *, unwrap: bool = False
    ) -> dict[str, Any]:
        """``POST /keys/<name>/<version>/<op>``."""
        return await call_json(
            self.client,
            TITLE,
            action.rpartition("/")[2],
            f"/keys/{quote(self.key_name, safe='')}/{action}?api-version={API_VERSION}",
            payload=payload,
            headers=await self._headers(action.rpartition("/")[2]),
            unwrap=unwrap,
        )
