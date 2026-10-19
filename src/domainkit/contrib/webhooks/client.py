"""Sending one webhook attempt, over httpx2 (the ``webhooks`` extra).

The request goes to the address the guard checked, never to a second
lookup: the URL's host is replaced by that address, and the host name is
kept for the ``Host`` header and for TLS (SNI and certificate checks).
Redirects are not followed, proxies from the environment are not used, the
whole attempt has ``TIMEOUT`` seconds, and at most ``MAX_RESPONSE_BYTES``
of the answer are read.
"""

import asyncio
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final

import httpx2

from .guard import UnsafeTarget, checked_address, checked_url
from .settings import WebhookSettings

MAX_ERROR_LENGTH: Final = 500


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one attempt got."""

    status: int | None
    """The answer's status code; None when there was no answer."""
    seconds: float
    error: str | None = None
    """Why the attempt failed; None when it succeeded."""
    refused: bool = False
    """The guard refused the URL: trying again cannot help."""

    @property
    def ok(self) -> bool:
        """Whether the receiver took the delivery (a 2xx answer)."""
        return self.status is not None and 200 <= self.status < 300


@cache
def _tls(ca_bundle: Path | None) -> ssl.SSLContext:
    """A client TLS context, built once per CA bundle."""
    if ca_bundle is None:
        context = httpx2.create_ssl_context(trust_env=False)
    else:
        context = ssl.create_default_context(cafile=ca_bundle)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


async def post(
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    config: WebhookSettings,
    *,
    allow_http: bool,
) -> Outcome:
    """POST ``body`` to ``url`` once; never raises for the receiver's sake.

    Errors are described without the URL, which may hold a secret token.
    """
    started = time.perf_counter()

    def outcome(
        status: int | None, error: str | None, *, refused: bool = False
    ) -> Outcome:
        if error is not None:
            error = error.replace(url, "<url>")[:MAX_ERROR_LENGTH]
        return Outcome(status, time.perf_counter() - started, error, refused)

    try:
        async with asyncio.timeout(config.TIMEOUT):
            target = checked_url(
                url, allow_http=allow_http, allowed=config.ALLOWED_PRIVATE_NETWORKS
            )
            address = await checked_address(target, config.ALLOWED_PRIVATE_NETWORKS)
            status = await _send(target, str(address), body, headers, config)
    except UnsafeTarget as exc:
        return outcome(None, f"Refused: {exc}", refused=True)
    except TimeoutError:
        return outcome(None, f"No answer within {config.TIMEOUT:g}s.")
    except (httpx2.HTTPError, OSError) as exc:
        return outcome(None, f"{type(exc).__name__}: {exc}")
    if 300 <= status < 400:
        return outcome(status, f"HTTP {status}: redirects are not followed.")
    if not 200 <= status < 300:
        return outcome(status, f"HTTP {status}.")
    return outcome(status, None)


async def _send(
    url: httpx2.URL,
    address: str,
    body: bytes,
    headers: Mapping[str, str],
    config: WebhookSettings,
) -> int:
    """Send to ``address``, naming ``url``'s host; return the status code."""
    host = url.raw_host.decode("ascii")
    request_headers = {
        **headers,
        "Host": url.netloc.decode("ascii"),
        "Content-Type": "application/json",
        "User-Agent": config.USER_AGENT,
    }
    async with (
        httpx2.AsyncClient(
            verify=_tls(config.CA_BUNDLE),
            trust_env=False,
            follow_redirects=False,
            timeout=httpx2.Timeout(config.TIMEOUT, connect=config.CONNECT_TIMEOUT),
        ) as client,
        client.stream(
            "POST",
            url.copy_with(host=address),
            content=body,
            headers=request_headers,
            extensions={"sni_hostname": host},
        ) as response,
    ):
        read = 0
        async for chunk in response.aiter_raw():
            read += len(chunk)
            if read >= config.MAX_RESPONSE_BYTES:
                break  # Closing the stream drops the rest with the connection.
        return response.status_code
