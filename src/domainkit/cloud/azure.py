"""Microsoft Entra ID tokens, and Azure Storage's Shared Key (``domainkit[cloud]``).

::

    app = EntraApplication(tenant_id, client_id, client_secret)
    token = await app.token("https://vault.azure.net/.default")

``EntraApplication`` asks Entra ID for a token with an application's
client secret (the client credentials grant); ``ManagedIdentity`` asks
the instance metadata service an Azure VM, AKS pod, or App Service has.
Tokens are kept until five minutes before they expire, per scope.

``shared_key`` signs a request to Azure Storage with the account's key,
and ``blob_sas`` makes a service SAS for one blob: what a presigned URL
is on Azure.
"""

import base64
import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime
from typing import Final
from urllib.parse import parse_qsl, unquote, urlsplit

from domainkit.cloud import CloudAuthError, TokenSource, http_errors, oauth_token

__all__ = ["EntraApplication", "ManagedIdentity", "blob_sas", "shared_key"]

AUTHORITY: Final = "https://login.microsoftonline.com"
IMDS: Final = "http://169.254.169.254/metadata/identity/oauth2/token"
SAS_VERSION: Final = "2022-11-02"
_SIGNED_STANDARD: Final = (
    "content-encoding",
    "content-language",
    "content-length",
    "content-md5",
    "content-type",
    "date",
    "if-modified-since",
    "if-match",
    "if-none-match",
    "if-unmodified-since",
    "range",
)
"""The standard headers Shared Key signs, in its order."""


class EntraApplication(TokenSource):
    """An application registered in Entra ID, with a client secret."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        *,
        authority: str = AUTHORITY,
    ) -> None:
        """Ask ``authority`` (a sovereign cloud's, or Entra's) as ``client_id``."""
        super().__init__()
        self.tenant_id = tenant_id
        self.client_id = client_id
        self._secret = client_secret
        self.authority = authority.rstrip("/")

    def __repr__(self) -> str:
        """The tenant and the client only: the secret grants access."""
        return (
            f"EntraApplication(tenant_id={self.tenant_id!r}, "
            f"client_id={self.client_id!r})"
        )

    async def _fetch(self, scope: str) -> tuple[str, float]:
        """The client credentials grant."""
        from domainkit.http.client import HttpClient

        endpoint = HttpClient(propagate=False)
        try:
            response = await endpoint.post(
                f"{self.authority}/{self.tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self._secret,
                    "scope": scope,
                },
                retry=True,
            )
        except http_errors() as exc:
            raise CloudAuthError("Entra ID could not be reached.") from exc
        finally:
            await endpoint.aclose()
        return oauth_token(response, "Entra ID")


class ManagedIdentity(TokenSource):
    """The identity Azure gives this host, through its instance metadata service."""

    def __init__(self, *, client_id: str | None = None, url: str = IMDS) -> None:
        """Use the host's identity, or the user-assigned one ``client_id`` names."""
        super().__init__()
        self.client_id = client_id
        self.url = url

    async def _fetch(self, scope: str) -> tuple[str, float]:
        """``GET`` the metadata service's token, with ``Metadata: true``."""
        from domainkit.http.client import HttpClient

        params = {
            "api-version": "2018-02-01",
            "resource": scope.removesuffix("/.default"),
        }
        if self.client_id:
            params["client_id"] = self.client_id
        server = HttpClient(propagate=False, breaker=False)
        try:
            response = await server.get(
                self.url, params=params, headers={"Metadata": "true"}
            )
        except http_errors() as exc:
            raise CloudAuthError(
                "The instance metadata service could not be reached."
            ) from exc
        finally:
            await server.aclose()
        return oauth_token(response, "The instance metadata service")


def shared_key(
    method: str,
    url: str,
    headers: Mapping[str, str],
    *,
    account: str,
    key: str,
) -> str:
    """The ``Authorization`` header Azure Storage's Shared Key gives this request.

    ``headers`` are the ones sent, ``x-ms-date`` and ``x-ms-version``
    among them; ``key`` is the account key, as Azure shows it (base64).

    Raises:
        CloudAuthError: The key is not base64.
    """
    lowered = {name.lower(): value for name, value in headers.items()}
    standard = [lowered.get(name, "") for name in _SIGNED_STANDARD]
    if standard[2] == "0":
        standard[2] = ""  # Azure signs an empty body's length as empty.
    canonical_headers = "".join(
        f"{name}:{' '.join(lowered[name].split())}\n"
        for name in sorted(lowered)
        if name.startswith("x-ms-")
    )
    parts = urlsplit(url)
    resource = f"/{account}{parts.path or '/'}"
    grouped: dict[str, list[str]] = {}
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        grouped.setdefault(name.lower(), []).append(unquote(value))
    for name in sorted(grouped):
        resource += f"\n{name}:{','.join(sorted(grouped[name]))}"
    to_sign = (
        "\n".join([method.upper(), *standard]) + "\n" + canonical_headers + resource
    )
    signature = _hmac(key, to_sign)
    return f"SharedKey {account}:{signature}"


def blob_sas(
    *,
    account: str,
    container: str,
    blob: str,
    key: str,
    permissions: str,
    expiry: datetime,
    version: str = SAS_VERSION,
) -> str:
    """A service SAS for one blob: the query string to append to its URL.

    ``permissions`` are SAS letters (``r`` to read, ``cw`` to create and
    write); https only.

    Raises:
        CloudAuthError: The key is not base64.
    """
    expires = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
    resource = f"/blob/{account}/{container}/{blob}"
    to_sign = "\n".join(
        [
            permissions,
            "",  # Start: now.
            expires,
            resource,
            "",  # Stored access policy.
            "",  # IP range.
            "https",
            version,
            "b",
            "",  # Snapshot time.
            "",  # Encryption scope.
            "",  # Cache-Control, Content-Disposition, -Encoding, -Language, -Type.
            "",
            "",
            "",
            "",
        ]
    )
    from urllib.parse import quote

    fields = {
        "sv": version,
        "sr": "b",
        "sp": permissions,
        "se": expires,
        "spr": "https",
        "sig": _hmac(key, to_sign),
    }
    return "&".join(f"{name}={quote(value, safe='')}" for name, value in fields.items())


def _hmac(key: str, text: str) -> str:
    """Base64 of HMAC-SHA256 of ``text`` under the base64 ``key``.

    Raises:
        CloudAuthError: The key is not base64.
    """
    try:
        secret = base64.b64decode(key, validate=True)
    except ValueError:
        raise CloudAuthError("The Azure Storage account key is not base64.") from None
    digest = hmac.new(secret, text.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()
