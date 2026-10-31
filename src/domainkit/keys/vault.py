"""HashiCorp Vault's transit engine as a ``KeyProvider``.

::

    provider = VaultTransitKeyProvider(
        "https://vault.internal:8200", "payments", token=env.str("VAULT_TOKEN")
    )

Data keys come from ``transit/datakey/plaintext/<key>``, are unwrapped by
``transit/decrypt/<key>``, and moved to the key's newest version by
``transit/rewrap/<key>``: plain HTTPS through ``domainkit.http.client``,
with the token in ``X-Vault-Token`` (and ``X-Vault-Namespace`` for Vault
Enterprise). A wrapped key is Vault's own ciphertext, ``vault:v<N>:...``,
which names the version of the transit key that wrapped it. After
``vault write -f transit/keys/<key>/rotate``, ``rewrap`` moves data keys
to the new version; raise the key's ``min_decryption_version`` once none
is left on an older one.

``token`` is a string, or an async callable that returns one (for a token
renewed by an agent): called before each request, so a renewed token is
used at once. The token needs ``update`` on the three paths above.
"""

import base64
import binascii
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final
from urllib.parse import quote

from domainkit.keys import DataKey, KeyProvider, KeyProviderError, UnknownKey

__all__ = [
    "VaultTransitKeyProvider",
]

NAME: Final = "vault"
UNWRAP_REFUSED: Final = 400
"""Vault's status for a ciphertext the key cannot decrypt."""

type Token = str | Callable[[], Awaitable[str]]


class VaultTransitKeyProvider(KeyProvider):
    """Data keys under one key of Vault's transit engine (see the module)."""

    name = NAME

    def __init__(
        self,
        url: str,
        key: str,
        *,
        token: Token,
        mount: str = "transit",
        namespace: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Use the transit key ``key`` at ``url``, mounted at ``mount``.

        Raises:
            ValueError: ``key`` or ``mount`` is empty.
        """
        from domainkit.http.client import HttpClient

        if not key or not mount:
            raise ValueError("A Vault transit key needs a key name and a mount.")
        self.url = url
        self.key = key
        self.mount = mount.strip("/")
        self.namespace = namespace
        self._token = token
        self.client = HttpClient(base_url=url, timeout=timeout, propagate=False)

    def __repr__(self) -> str:
        """The server and key; never the token."""
        return f"VaultTransitKeyProvider(url={self.url!r}, key={self.key!r})"

    async def generate_data_key(self) -> DataKey:
        """A new 256-bit data key, and Vault's ciphertext of it."""
        data = await self._call("datakey/plaintext", {"bits": 256})
        return DataKey(_plaintext(data), _ciphertext(data))

    async def unwrap(self, wrapped: str) -> bytes:
        """The data key Vault's ciphertext ``wrapped`` holds."""
        data = await self._call("decrypt", {"ciphertext": self._owned(wrapped)})
        return _plaintext(data)

    async def rewrap(self, wrapped: str) -> str:
        """``wrapped`` under the transit key's newest version, inside Vault."""
        data = await self._call("rewrap", {"ciphertext": self._owned(wrapped)})
        return _ciphertext(data)

    def _owned(self, wrapped: str) -> str:
        """``wrapped``, if Vault made it.

        Raises:
            UnknownKey: It is not Vault's ciphertext.
        """
        if not self.owns(wrapped):
            raise UnknownKey("Vault did not wrap that key.")
        return wrapped

    async def _headers(self) -> dict[str, str]:
        """The token, and the namespace if there is one."""
        token = self._token if isinstance(self._token, str) else await self._token()
        headers = {"X-Vault-Token": token}
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        return headers

    async def _call(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """``POST /v1/<mount>/<action>/<key>``; the answer's ``data``.

        Raises:
            UnknownKey: Vault could not decrypt the ciphertext.
            KeyProviderError: Vault refused otherwise, or could not be reached.
        """
        import httpx2

        path = f"/v1/{quote(self.mount)}/{action}/{quote(self.key, safe='')}"
        try:
            response = await self.client.post(
                path, json=dict(payload), headers=await self._headers(), retry=True
            )
        except httpx2.HTTPError as exc:
            raise KeyProviderError(f"Vault could not be reached ({action}).") from exc
        status = response.status_code
        if status == UNWRAP_REFUSED and action in {"decrypt", "rewrap"}:
            raise UnknownKey(f"Vault cannot unwrap that key with {self.key!r}.")
        if status != 200:
            raise KeyProviderError(f"Vault refused {action} ({status}).")
        try:
            data = response.json()["data"]
        except (ValueError, KeyError, TypeError):
            data = None
        if not isinstance(data, dict):
            raise KeyProviderError(f"Vault answered {action} without data.")
        return data


def _plaintext(data: Mapping[str, Any]) -> bytes:
    """The data key in an answer.

    Raises:
        KeyProviderError: It is missing, or not base64.
    """
    value = data.get("plaintext")
    try:
        if not isinstance(value, str):
            raise ValueError
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise KeyProviderError("Vault answered without the key.") from None


def _ciphertext(data: Mapping[str, Any]) -> str:
    """The ciphertext in an answer.

    Raises:
        KeyProviderError: It is missing, or not Vault's.
    """
    value = data.get("ciphertext")
    if not isinstance(value, str) or not value.startswith(f"{NAME}:"):
        raise KeyProviderError("Vault answered without the wrapped key.")
    return value
