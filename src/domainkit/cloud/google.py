"""OAuth tokens for a Google service account (``domainkit[cloud]``).

::

    account = ServiceAccount.from_file("/run/secrets/gcp.json")
    token = await account.token("https://www.googleapis.com/auth/devstorage.read_write")

A key file (the JSON Google gives for a service account) signs a JWT
with its RSA key, and Google's token endpoint trades it for an access
token (RFC 7523). On Google Cloud without a key file (GKE's workload
identity, Compute Engine), ``MetadataAccount`` asks the metadata server.
Tokens are kept until five minutes before they expire, per scope.

A key file can also sign bytes (``sign``), which V4 signed URLs need;
the metadata server cannot, so presigned URLs need a key file.
"""

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Self

from domainkit import clock
from domainkit.cloud import CloudAuthError, TokenSource, http_errors, oauth_token

__all__ = ["GoogleAccount", "MetadataAccount", "ServiceAccount"]

TOKEN_URI: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL.
METADATA: Final = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default"
)
GRANT: Final = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class GoogleAccount(TokenSource):
    """Who calls Google's APIs: an email, and access tokens per scope."""

    email: str

    def sign(self, data: bytes) -> bytes:
        """``data`` signed with RSA-SHA256 by the account's key.

        Raises:
            CloudAuthError: The account has no key here to sign with.
        """
        raise CloudAuthError(
            f"{type(self).__name__} cannot sign: use a service account key file."
        )


class ServiceAccount(GoogleAccount):
    """A service account from its key file: it signs its own token requests."""

    def __init__(
        self,
        email: str,
        private_key: str,
        *,
        token_uri: str = TOKEN_URI,
        key_id: str | None = None,
    ) -> None:
        """Use the account ``email`` with its PEM ``private_key``.

        Raises:
            CloudAuthError: The key is not an RSA private key.
        """
        super().__init__()
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        try:
            key = serialization.load_pem_private_key(private_key.encode(), None)
        except (ValueError, TypeError):
            raise CloudAuthError(
                "The service account's private key could not be read."
            ) from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise CloudAuthError("A service account's private key is an RSA key.")
        self.email = email
        self.token_uri = token_uri
        self.key_id = key_id
        self._key = key

    @classmethod
    def from_info(cls, info: Mapping[str, Any]) -> Self:
        """The account a key file's contents describe.

        Raises:
            CloudAuthError: It is not a service account key.
        """
        try:
            if info["type"] != "service_account":
                raise KeyError("type")
            return cls(
                info["client_email"],
                info["private_key"],
                token_uri=info.get("token_uri", TOKEN_URI),
                key_id=info.get("private_key_id"),
            )
        except (KeyError, TypeError):
            raise CloudAuthError("That is not a service account key file.") from None

    @classmethod
    def from_file(cls, path: str | Path) -> Self:
        """The account of the key file at ``path``.

        Raises:
            CloudAuthError: The file is missing, or not a service account key.
        """
        try:
            info = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CloudAuthError(
                "The service account key file could not be read."
            ) from None
        return cls.from_info(info)

    def sign(self, data: bytes) -> bytes:
        """``data`` signed with RSA-SHA256 (PKCS#1 v1.5), as Google verifies it."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        return self._key.sign(data, padding.PKCS1v15(), hashes.SHA256())

    def assertion(self, scope: str, now: float) -> str:
        """The JWT that asks Google for a token for ``scope``, valid an hour."""
        header = {"alg": "RS256", "typ": "JWT"}
        if self.key_id:
            header["kid"] = self.key_id
        claims = {
            "iss": self.email,
            "scope": scope,
            "aud": self.token_uri,
            "iat": int(now),
            "exp": int(now) + 3600,
        }
        signing_input = (
            f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
        )
        return f"{signing_input}.{_b64(self.sign(signing_input.encode()))}"

    async def _fetch(self, scope: str) -> tuple[str, float]:
        """Trade a signed assertion for a token."""
        from domainkit.http.client import HttpClient

        token_endpoint = HttpClient(propagate=False)
        try:
            response = await token_endpoint.post(
                self.token_uri,
                data={
                    "grant_type": GRANT,
                    "assertion": self.assertion(scope, clock.now().timestamp()),
                },
                retry=True,
            )
        except http_errors() as exc:
            raise CloudAuthError(
                "Google's token endpoint could not be reached."
            ) from exc
        finally:
            await token_endpoint.aclose()
        return oauth_token(response, "Google's token endpoint")


class MetadataAccount(GoogleAccount):
    """The account Google Cloud runs this code as, through the metadata server."""

    def __init__(self, *, url: str = METADATA) -> None:
        """Ask the metadata server at ``url`` (its default on Google Cloud)."""
        super().__init__()
        self.url = url
        self.email = "default"

    async def _fetch(self, scope: str) -> tuple[str, float]:
        """``GET .../token?scopes=``, with ``Metadata-Flavor: Google``."""
        from domainkit.http.client import HttpClient

        server = HttpClient(propagate=False, breaker=False)
        try:
            response = await server.get(
                f"{self.url}/token",
                params={"scopes": scope},
                headers={"Metadata-Flavor": "Google"},
            )
        except http_errors() as exc:
            raise CloudAuthError("The metadata server could not be reached.") from exc
        finally:
            await server.aclose()
        return oauth_token(response, "The metadata server")


def _b64(data: bytes) -> str:
    """Unpadded base64url, as JWTs write their parts."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
