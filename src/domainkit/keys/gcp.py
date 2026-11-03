"""Google Cloud KMS as a ``KeyProvider``.

::

    provider = GcpKmsKeyProvider(
        "projects/acme/locations/europe-west1/keyRings/app/cryptoKeys/fields"
    )

Cloud KMS makes no data keys: a data key is 32 random bytes made here,
wrapped by the key's ``encrypt`` (under its primary version), and
unwrapped by ``decrypt``, with ``domainkit.keys`` as additional
authenticated data so a ciphertext made for another use never unwraps.
``rewrap`` unwraps and wraps again, under the version now primary. The
caller is a service account (``credentials_file``) or, on Google Cloud,
the metadata server's account, with ``roles/cloudkms.cryptoKeyEncrypterDecrypter``
on the key. A wrapped key is ``gcp-kms:`` and Cloud KMS's ciphertext.
"""

import base64
import binascii
import secrets
from typing import Any, Final

from domainkit.cloud import TokenSource

from . import DATA_KEY_BYTES, DataKey, KeyProvider, KeyProviderError, UnknownKey
from ._http import call_json, field

__all__ = ["GcpKmsKeyProvider"]

NAME: Final = "gcp-kms"
SCOPE: Final = "https://www.googleapis.com/auth/cloudkms"
AAD: Final = base64.b64encode(b"domainkit.keys").decode()
TITLE: Final = "Cloud KMS"


class GcpKmsKeyProvider(KeyProvider):
    """Data keys under one Cloud KMS key (see the module)."""

    name = NAME

    def __init__(
        self,
        key_name: str,
        *,
        account: TokenSource | None = None,
        credentials_file: str | None = None,
        endpoint: str = "https://cloudkms.googleapis.com",
        timeout: float | None = None,
    ) -> None:
        """Use ``key_name`` (``projects/.../cryptoKeys/<key>``), called as ``account``.

        Raises:
            ValueError: ``key_name`` is not a key's full name.
        """
        from domainkit.cloud.google import MetadataAccount, ServiceAccount
        from domainkit.http.client import HttpClient

        parts = key_name.split("/")
        if len(parts) != 8 or parts[::2] != [
            "projects",
            "locations",
            "keyRings",
            "cryptoKeys",
        ]:
            raise ValueError(
                "A Cloud KMS key is named projects/<p>/locations/<l>/keyRings/<r>"
                "/cryptoKeys/<k>."
            )
        self.key_name = key_name
        if account is None:
            account = (
                MetadataAccount()
                if credentials_file is None
                else ServiceAccount.from_file(credentials_file)
            )
        self.account = account
        self.client = HttpClient(
            base_url=endpoint.rstrip("/"), timeout=timeout, propagate=False
        )

    def __repr__(self) -> str:
        """The key's name; no credential."""
        return f"GcpKmsKeyProvider(key_name={self.key_name!r})"

    async def generate_data_key(self) -> DataKey:
        """32 random bytes, and them encrypted by the key's primary version."""
        plaintext = secrets.token_bytes(DATA_KEY_BYTES)
        return DataKey(plaintext, await self._wrap(plaintext))

    async def unwrap(self, wrapped: str) -> bytes:
        """``decrypt``: the data key ``wrapped`` holds."""
        if not self.owns(wrapped):
            raise UnknownKey("Cloud KMS did not wrap that key.")
        answer = await self._call(
            "decrypt",
            {
                "ciphertext": wrapped.removeprefix(f"{NAME}:"),
                "additionalAuthenticatedData": AAD,
            },
        )
        try:
            plaintext = base64.b64decode(
                field(answer, "plaintext", TITLE), validate=True
            )
        except (binascii.Error, ValueError):
            raise KeyProviderError("Cloud KMS answered without the key.") from None
        return plaintext

    async def rewrap(self, wrapped: str) -> str:
        """Unwrapped, then wrapped by the version now primary."""
        return await self._wrap(await self.unwrap(wrapped))

    async def _wrap(self, plaintext: bytes) -> str:
        """``encrypt``: ``gcp-kms:`` and the ciphertext."""
        answer = await self._call(
            "encrypt",
            {
                "plaintext": base64.b64encode(plaintext).decode(),
                "additionalAuthenticatedData": AAD,
            },
        )
        return f"{NAME}:{field(answer, 'ciphertext', TITLE)}"

    async def _call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /v1/<key>:<action>`` with the account's token."""
        from domainkit.cloud import CloudAuthError

        try:
            token = await self.account.token(SCOPE)
        except CloudAuthError as exc:
            raise KeyProviderError(
                f"Cloud KMS's token was refused ({action})."
            ) from exc
        return await call_json(
            self.client,
            TITLE,
            action,
            f"/v1/{self.key_name}:{action}",
            payload=payload,
            headers={"Authorization": f"Bearer {token}"},
            unwrap=action == "decrypt",
        )
