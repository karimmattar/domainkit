"""Which webhook URLs may be called: the guard against server-side request forgery.

A subscription's URL is checked when it is saved and again before every
attempt, by the shared guard (``domainkit.http.guard``):

- ``https`` only (``http`` too while ``DEBUG`` is on), with a host, and
  without a user name, password, or fragment;
- not ``localhost`` (or ``*.localhost``) or a cloud metadata host name;
- every address the host resolves to must be public: no private, loopback,
  link-local, shared (CGNAT), reserved, multicast, or unspecified address,
  in IPv4 or IPv6. An IPv4 address inside an IPv6 one (``::ffff:10.0.0.1``,
  NAT64's ``64:ff9b::/96``) is judged as the IPv4 address it holds.
  ``WEBHOOKS.ALLOWED_PRIVATE_NETWORKS`` lets named private networks through;
  cloud metadata addresses never are.

The address that was checked is the one connected to (see ``client``), so
a host that resolves differently a moment later (DNS rebinding) gains
nothing.
"""

from collections.abc import Sequence
from typing import Final

import httpx2

from domainkit.http import guard
from domainkit.http.guard import (
    MAX_URL_LENGTH,
    METADATA_ADDRESSES,
    METADATA_HOSTS,
    NAT64,
    Address,
    Network,
    UnsafeURL,
    judged,
    lookup,
    refusal,
)

__all__ = [
    "MAX_URL_LENGTH",
    "METADATA_ADDRESSES",
    "METADATA_HOSTS",
    "NAT64",
    "Address",
    "Network",
    "UnsafeTarget",
    "checked_address",
    "checked_url",
    "judged",
    "lookup",
    "refusal",
]

SUBJECT: Final = "The webhook URL"


class UnsafeTarget(UnsafeURL):
    """A webhook URL that may not be called; the message never quotes it."""

    code = "unsafe_webhook_url"


def checked_url(
    url: str, *, allow_http: bool, allowed: Sequence[Network] = ()
) -> httpx2.URL:
    """Parse ``url`` and check everything that does not need a lookup.

    Raises:
        UnsafeTarget: the URL may not be called.
    """
    return guard.checked_url(
        url,
        allow_http=allow_http,
        allowed=allowed,
        subject=SUBJECT,
        error=UnsafeTarget,
        https_only=" (http only while DEBUG is on).",
    )


async def checked_address(url: httpx2.URL, allowed: Sequence[Network] = ()) -> Address:
    """The address to connect to for ``url``, once all of its host's pass.

    Raises:
        UnsafeTarget: one of the addresses may not be called, or there is
            none.
        OSError: the host could not be resolved.
    """
    return await guard.checked_address(
        url, allowed, resolve=lookup, subject=SUBJECT, error=UnsafeTarget
    )
