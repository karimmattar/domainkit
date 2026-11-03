"""Calls to AWS's APIs, signed with SigV4 (``domainkit.keys.aws``).

Credentials are a key pair given, or the first source the environment
sets up (``default_credentials``: variables, a web identity, a container
role, the instance's role).
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING

from domainkit import clock
from domainkit.keys.aws import (
    AwsCredentials,
    CredentialSource,
    default_credentials,
    sign,
)

if TYPE_CHECKING:
    import httpx2

    from domainkit.http.client import HttpClient

__all__ = ["credential_source", "signed_request"]


def credential_source(
    region: str,
    access_key_id: str | None = None,
    secret_access_key: str | None = None,
) -> CredentialSource:
    """The key pair given, or the environment's credentials."""
    if access_key_id is None or secret_access_key is None:
        return default_credentials(region)
    static = AwsCredentials(access_key_id, secret_access_key)

    async def fixed() -> AwsCredentials:
        return static

    return fixed


async def signed_request(
    client: "HttpClient",
    method: str,
    url: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
    region: str,
    service: str,
    credentials: CredentialSource,
) -> "httpx2.Response":
    """Send ``body`` to ``url``, signed for ``service`` in ``region``."""
    signed = sign(
        method,
        url,
        headers,
        body,
        await credentials(),
        region=region,
        service=service,
        now=clock.now(),
    )
    return await client.request(method, url, content=body, headers=signed)
