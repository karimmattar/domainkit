"""Credentials for cloud services, shared by the adapters that call them.

- ``domainkit.cloud.google``: OAuth tokens for a Google service account,
  from its key file or the metadata server (GCS, Cloud KMS, FCM).
- ``domainkit.cloud.azure``: Microsoft Entra ID tokens for an application
  or a managed identity, and Azure Storage's Shared Key (Blob storage, Key
  Vault).

AWS's Signature Version 4 and credential chain are in
``domainkit.keys.aws``, which S3, SQS, SNS, and SES use. Every call goes
through ``domainkit.http.client``; no cloud SDK is needed.
"""

from abc import ABC, abstractmethod
from typing import Any, Final

from domainkit import clock
from domainkit.exceptions import DomainkitError

__all__ = ["CloudAuthError", "TokenSource"]

REFRESH_BEFORE: Final = 300
"""Seconds before a token expires that a new one is asked for."""


class CloudAuthError(DomainkitError):
    """A cloud service refused the credentials, or could not be asked for a token.

    The message names the service and what failed, never a key or a token.
    """


class TokenSource(ABC):
    """Access tokens per scope, each kept until five minutes before it expires."""

    def __init__(self) -> None:
        """Hold no token yet."""
        self._tokens: dict[str, tuple[str, float]] = {}

    async def token(self, scope: str) -> str:
        """An access token for ``scope``, new or held.

        Raises:
            CloudAuthError: The service refused, or could not be reached.
        """
        now = clock.now().timestamp()
        held = self._tokens.get(scope)
        if held is not None and held[1] - REFRESH_BEFORE > now:
            return held[0]
        token, lifetime = await self._fetch(scope)
        self._tokens[scope] = (token, now + lifetime)
        return token

    @abstractmethod
    async def _fetch(self, scope: str) -> tuple[str, float]:
        """A new token for ``scope``, and how many seconds it works."""


def oauth_token(response: Any, source: str) -> tuple[str, float]:  # noqa: ANN401 - httpx2's.
    """The token and lifetime of an OAuth 2 token answer.

    Raises:
        CloudAuthError: It refused, or answered without a token.
    """
    if response.status_code != 200:
        raise CloudAuthError(f"{source} refused a token ({response.status_code}).")
    try:
        document = response.json()
        return str(document["access_token"]), float(document.get("expires_in", 3600))
    except (ValueError, KeyError, TypeError):
        raise CloudAuthError(f"{source} answered without a token.") from None


def http_errors() -> tuple[type[BaseException], ...]:
    """The failures of a call that mean the service could not be reached."""
    import httpx2

    return (httpx2.HTTPError,)
