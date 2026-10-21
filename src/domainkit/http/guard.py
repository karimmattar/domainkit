"""Which URLs a server may call: the guard against server-side request forgery.

A URL chosen by someone else (a webhook subscription, a callback, a link to
fetch) is checked before it is called:

- ``https`` only (``http`` too where allowed), with a host, and without a
  user name, password, or fragment;
- not ``localhost`` (or ``*.localhost``) or a cloud metadata host name;
- every address the host resolves to must be public: no private, loopback,
  link-local, shared (CGNAT), reserved, multicast, or unspecified address,
  in IPv4 or IPv6. An IPv4 address inside an IPv6 one (``::ffff:10.0.0.1``,
  NAT64's ``64:ff9b::/96``) is judged as the IPv4 address it holds.
  ``allowed`` networks let named private networks through; cloud metadata
  addresses never are.

``checked_url`` checks what the URL shows; ``checked_host`` (and
``checked_address`` for a URL) resolves the host and checks every address.
Callers connect to the address that was checked, never to a second lookup,
so a host that resolves differently a moment later (DNS rebinding) gains
nothing. ``domainkit.contrib.webhooks`` checks subscriptions with it, and
``domainkit.http.client`` the calls of a client made with ``guard=True``.

Error messages never quote the URL, which may hold a secret token.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

import httpx2

from domainkit.ddd import BusinessRuleViolation

type Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type Network = ipaddress.IPv4Network | ipaddress.IPv6Network
type Resolver = Callable[[str, int], Awaitable[list[Address]]]

MAX_URL_LENGTH: Final = 2048
METADATA_ADDRESSES: Final = frozenset(
    ipaddress.ip_address(address)
    for address in (
        "169.254.169.254",  # AWS, Azure, Google Cloud, OpenStack, DigitalOcean
        "169.254.170.2",  # AWS ECS task metadata
        "fd00:ec2::254",  # AWS over IPv6
        "100.100.100.200",  # Alibaba Cloud
        "168.63.129.16",  # Azure platform (a public address)
        "192.0.0.192",  # Oracle Cloud
    )
)
METADATA_HOSTS: Final = frozenset(
    {
        "localhost",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
NAT64: Final = ipaddress.IPv6Network("64:ff9b::/96")
SUBJECT: Final = "The URL"
"""How messages name what was refused, unless the caller names it otherwise."""


class UnsafeURL(BusinessRuleViolation):
    """A URL that may not be called; the message never quotes it."""

    code = "unsafe_url"


def judged(address: Address) -> Address:
    """The address whose kind decides: an IPv4 address inside an IPv6 one."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address in NAT64:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def refusal(address: Address, allowed: Sequence[Network] = ()) -> str | None:
    """Why ``address`` may not be called, or None if it may."""
    inner = judged(address)
    if address in METADATA_ADDRESSES or inner in METADATA_ADDRESSES:
        return "a cloud metadata address"
    if inner.is_unspecified or inner.is_multicast:
        return "an unspecified or multicast address"
    if any(inner in network for network in allowed):
        return None
    if (
        not inner.is_global
        or inner.is_reserved
        or (isinstance(inner, ipaddress.IPv6Address) and inner.is_site_local)
    ):
        return "a private, loopback, link-local, or reserved address"
    return None


def _literal(host: str) -> Address | None:
    """The address ``host`` spells, if it is one."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def checked_url(
    url: str,
    *,
    allow_http: bool,
    allowed: Sequence[Network] = (),
    subject: str = SUBJECT,
    error: type[UnsafeURL] = UnsafeURL,
    https_only: str = ".",
) -> httpx2.URL:
    """Parse ``url`` and check everything that does not need a lookup.

    Messages start with ``subject``; ``https_only`` ends the one that asks
    for https when http is not allowed.

    Raises:
        UnsafeURL: The URL may not be called (an ``error``).
    """
    if len(url) > MAX_URL_LENGTH:
        raise error(f"{subject} is longer than {MAX_URL_LENGTH} characters.")
    try:
        parsed = httpx2.URL(url)
    except httpx2.InvalidURL:
        raise error(f"{subject} is not a valid URL.") from None
    schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme not in schemes:
        raise error(
            f"{subject} must use https" + (" or http." if allow_http else https_only)
        )
    if parsed.userinfo:
        raise error(f"{subject} may not hold a user name or password.")
    if parsed.port is not None and not 0 < parsed.port < 65536:
        raise error(f"{subject}'s port is not valid.")
    if parsed.fragment:
        raise error(f"{subject} may not have a fragment (#...).")
    host = parsed.raw_host.decode("ascii").rstrip(".").lower()
    if not host:
        raise error(f"{subject} has no host.")
    if host in METADATA_HOSTS or host.endswith(".localhost"):
        raise error(f"{subject} names a local or metadata host.")
    literal = _literal(host)
    if literal is not None and (reason := refusal(literal, allowed)):
        raise error(f"{subject}'s host is {reason}.")
    return parsed


async def lookup(host: str, port: int) -> list[Address]:
    """Every address ``host`` resolves to, in the resolver's order."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return list(dict.fromkeys(ipaddress.ip_address(info[4][0]) for info in infos))


async def checked_host(
    host: str,
    port: int,
    allowed: Sequence[Network] = (),
    *,
    resolve: Resolver | None = None,
    subject: str = SUBJECT,
    error: type[UnsafeURL] = UnsafeURL,
) -> Address:
    """The address to connect to for ``host``, once all of its addresses pass.

    ``resolve`` looks the host up (default: ``lookup``, the system resolver).

    Raises:
        UnsafeURL: One of the addresses may not be called, or there is none
            (an ``error``).
        OSError: The host could not be resolved.
    """
    literal = _literal(host)
    if literal is not None:
        addresses = [literal]
    else:
        addresses = await (resolve or lookup)(host, port)
    if not addresses:
        raise error(f"{subject}'s host has no address.")
    for address in addresses:
        if reason := refusal(address, allowed):
            raise error(f"{subject}'s host resolves to {reason}.")
    return addresses[0]


async def checked_address(
    url: httpx2.URL,
    allowed: Sequence[Network] = (),
    *,
    resolve: Resolver | None = None,
    subject: str = SUBJECT,
    error: type[UnsafeURL] = UnsafeURL,
) -> Address:
    """The address to connect to for ``url`` (see ``checked_host``)."""
    port = url.port or (443 if url.scheme == "https" else 80)
    return await checked_host(
        url.raw_host.decode("ascii"),
        port,
        allowed,
        resolve=resolve,
        subject=subject,
        error=error,
    )
