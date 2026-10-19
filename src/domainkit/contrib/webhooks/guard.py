"""Which webhook URLs may be called: the guard against server-side request forgery.

A subscription's URL is checked when it is saved and again before every
attempt:

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

import asyncio
import ipaddress
import socket
from collections.abc import Sequence
from typing import Final

import httpx2

from domainkit.ddd import BusinessRuleViolation

type Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

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


class UnsafeTarget(BusinessRuleViolation):
    """A webhook URL that may not be called; the message never quotes it."""

    code = "unsafe_webhook_url"


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
    url: str, *, allow_http: bool, allowed: Sequence[Network] = ()
) -> httpx2.URL:
    """Parse ``url`` and check everything that does not need a lookup.

    Raises:
        UnsafeTarget: the URL may not be called.
    """
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeTarget(
            f"The webhook URL is longer than {MAX_URL_LENGTH} characters."
        )
    try:
        parsed = httpx2.URL(url)
    except httpx2.InvalidURL:
        raise UnsafeTarget("The webhook URL is not a valid URL.") from None
    schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme not in schemes:
        raise UnsafeTarget(
            "The webhook URL must use https"
            + (" or http." if allow_http else " (http only while DEBUG is on).")
        )
    if parsed.userinfo:
        raise UnsafeTarget("The webhook URL may not hold a user name or password.")
    if parsed.port is not None and not 0 < parsed.port < 65536:
        raise UnsafeTarget("The webhook URL's port is not valid.")
    if parsed.fragment:
        raise UnsafeTarget("The webhook URL may not have a fragment (#...).")
    host = parsed.raw_host.decode("ascii").rstrip(".").lower()
    if not host:
        raise UnsafeTarget("The webhook URL has no host.")
    if host in METADATA_HOSTS or host.endswith(".localhost"):
        raise UnsafeTarget("The webhook URL names a local or metadata host.")
    literal = _literal(host)
    if literal is not None and (reason := refusal(literal, allowed)):
        raise UnsafeTarget(f"The webhook URL's host is {reason}.")
    return parsed


async def lookup(host: str, port: int) -> list[Address]:
    """Every address ``host`` resolves to, in the resolver's order."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return list(dict.fromkeys(ipaddress.ip_address(info[4][0]) for info in infos))


async def checked_address(url: httpx2.URL, allowed: Sequence[Network] = ()) -> Address:
    """The address to connect to for ``url``, once all of its host's pass.

    Raises:
        UnsafeTarget: one of the addresses may not be called, or there is
            none.
        OSError: the host could not be resolved.
    """
    host = url.raw_host.decode("ascii")
    port = url.port or (443 if url.scheme == "https" else 80)
    literal = _literal(host)
    addresses = [literal] if literal is not None else await lookup(host, port)
    if not addresses:
        raise UnsafeTarget("The webhook URL's host has no address.")
    for address in addresses:
        if reason := refusal(address, allowed):
            raise UnsafeTarget(f"The webhook URL's host resolves to {reason}.")
    return addresses[0]
