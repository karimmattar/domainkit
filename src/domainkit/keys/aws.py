"""AWS KMS as a ``KeyProvider``: data keys under a KMS key, signed with SigV4.

::

    provider = AwsKmsKeyProvider(
        "arn:aws:kms:eu-west-1:111122223333:key/1234abcd-...", region="eu-west-1"
    )

Each call is one HTTPS request to KMS's JSON API (``GenerateDataKey``,
``Decrypt``, ``ReEncrypt``), signed with Signature Version 4 here, through
``domainkit.http.client``: no AWS SDK is needed (ADR 0119). Every call
names the key, and carries the encryption context
``{"domainkit": "data-key"}``, which KMS binds to the wrapped key and
CloudTrail records.

Credentials come from ``credentials``, an async callable, by default
``default_credentials()``: the environment's, as ``domainkit.cloud.aws``
reads them for every AWS service (variables, a web identity, the
container's). Pass another callable for anything else (an EC2 instance
profile, say). The signing and the credential chain moved to
``domainkit.cloud.aws`` in 2.9, and are still importable from here.
"""

import base64
import binascii
import json
import re
from collections.abc import Mapping
from typing import Any, Final

from domainkit import clock
from domainkit.cloud import http_errors
from domainkit.cloud.aws import ALGORITHM as ALGORITHM
from domainkit.cloud.aws import AwsCredentials as AwsCredentials
from domainkit.cloud.aws import CredentialSource as CredentialSource
from domainkit.cloud.aws import container_credentials as container_credentials
from domainkit.cloud.aws import default_credentials as default_credentials
from domainkit.cloud.aws import environment_credentials as environment_credentials
from domainkit.cloud.aws import sign as sign
from domainkit.cloud.aws import signing_key as signing_key
from domainkit.cloud.aws import web_identity_credentials as web_identity_credentials
from domainkit.keys import DataKey, KeyProvider, KeyProviderError, UnknownKey

__all__ = [
    "AwsCredentials",
    "AwsKmsKeyProvider",
]

NAME: Final = "aws-kms"
JSON_1_1: Final = "application/x-amz-json-1.1"
CONTEXT: Final = {"domainkit": "data-key"}
"""The encryption context of every data key: KMS binds it to the wrapped key."""
UNWRAP_REFUSALS: Final = frozenset(
    {"InvalidCiphertextException", "IncorrectKeyException"}
)
"""KMS errors that mean the wrapped key is not one this key unwraps."""
_ERROR_TYPE: Final = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}")


class AwsKmsKeyProvider(KeyProvider):
    """Data keys under one AWS KMS key (see the module)."""

    name = NAME

    def __init__(
        self,
        key_id: str,
        *,
        region: str,
        endpoint: str | None = None,
        credentials: CredentialSource | None = None,
        timeout: float | None = None,
    ) -> None:
        """Use the KMS key ``key_id`` (an ARN, an id, or ``alias/...``) in ``region``.

        ``endpoint`` replaces ``https://kms.<region>.amazonaws.com`` (a VPC
        endpoint, FIPS, or a test double); ``credentials`` replaces
        ``default_credentials(region)``; ``timeout`` bounds each call.
        """
        from domainkit.http.client import HttpClient

        self.key_id = key_id
        self.region = region
        self.endpoint = (endpoint or f"https://kms.{region}.amazonaws.com").rstrip("/")
        self.credentials = credentials or default_credentials(region)
        self.client = HttpClient(
            base_url=self.endpoint, timeout=timeout, propagate=False
        )

    def __repr__(self) -> str:
        """The key and region; no credential."""
        return f"AwsKmsKeyProvider(key_id={self.key_id!r}, region={self.region!r})"

    async def generate_data_key(self) -> DataKey:
        """``GenerateDataKey``: 32 random bytes, and them under the KMS key."""
        answer = await self._call(
            "GenerateDataKey",
            {"KeyId": self.key_id, "KeySpec": "AES_256", "EncryptionContext": CONTEXT},
        )
        return DataKey(
            _bytes(answer, "Plaintext"), f"{NAME}:{_text(answer, 'CiphertextBlob')}"
        )

    async def unwrap(self, wrapped: str) -> bytes:
        """``Decrypt``, naming the key, so KMS refuses a blob of any other."""
        answer = await self._call(
            "Decrypt",
            {
                "CiphertextBlob": self._blob(wrapped),
                "KeyId": self.key_id,
                "EncryptionContext": CONTEXT,
            },
        )
        return _bytes(answer, "Plaintext")

    async def rewrap(self, wrapped: str) -> str:
        """``ReEncrypt`` to the KMS key's current version, inside KMS."""
        answer = await self._call(
            "ReEncrypt",
            {
                "CiphertextBlob": self._blob(wrapped),
                "SourceKeyId": self.key_id,
                "SourceEncryptionContext": CONTEXT,
                "DestinationKeyId": self.key_id,
                "DestinationEncryptionContext": CONTEXT,
            },
        )
        return f"{NAME}:{_text(answer, 'CiphertextBlob')}"

    def _blob(self, wrapped: str) -> str:
        """The KMS ciphertext ``wrapped`` holds, as base64.

        Raises:
            UnknownKey: It is not a key KMS wrapped.
        """
        if not self.owns(wrapped):
            raise UnknownKey("AWS KMS did not wrap that key.")
        blob = wrapped.removeprefix(f"{NAME}:")
        try:
            base64.b64decode(blob, validate=True)
        except (binascii.Error, ValueError):
            raise UnknownKey("The wrapped key was changed, or cut short.") from None
        return blob

    async def _call(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Send ``operation`` to KMS, signed; its answer.

        Raises:
            UnknownKey: KMS refused the ciphertext.
            KeyProviderError: KMS refused otherwise, or could not be reached.
        """
        body = json.dumps(payload).encode()
        url = f"{self.endpoint}/"
        headers = sign(
            "POST",
            url,
            {"Content-Type": JSON_1_1, "X-Amz-Target": f"TrentService.{operation}"},
            body,
            await self.credentials(),
            region=self.region,
            service="kms",
            now=clock.now(),
        )
        try:
            response = await self.client.post(
                "/", content=body, headers=headers, retry=True
            )
        except http_errors() as exc:
            raise KeyProviderError(
                f"AWS KMS could not be reached ({operation})."
            ) from exc
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise KeyProviderError(
                f"AWS KMS answered {operation} with {response.status_code} and no JSON."
            )
        if response.status_code == 200:
            return answer
        kind = str(answer.get("__type", "")).rpartition("#")[2]
        kind = kind if _ERROR_TYPE.fullmatch(kind) else "an error"
        if kind in UNWRAP_REFUSALS:
            raise UnknownKey(f"AWS KMS cannot unwrap that key ({kind}).")
        raise KeyProviderError(
            f"AWS KMS refused {operation}: {kind} ({response.status_code})."
        )


def _text(answer: Mapping[str, Any], name: str) -> str:
    """The base64 field ``name`` of an answer, checked.

    Raises:
        KeyProviderError: It is missing, or not base64.
    """
    value = answer.get(name)
    try:
        if not isinstance(value, str):
            raise ValueError
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise KeyProviderError(f"AWS KMS answered without {name}.") from None
    return value


def _bytes(answer: Mapping[str, Any], name: str) -> bytes:
    """The base64 field ``name`` of an answer, decoded."""
    return base64.b64decode(_text(answer, name))
