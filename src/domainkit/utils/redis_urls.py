"""Redis URLs: one server, a Sentinel group, or a Cluster.

``redis://`` and ``rediss://`` name one server (redis-py reads them). A
Sentinel group and a Cluster have several addresses, which ``urlsplit``
cannot read, so they have their own schemes, read here::

    redis+sentinel://:password@sentinel-1:26379,sentinel-2:26379/mymaster/0
    rediss+cluster://user:password@node-1:6379,node-2:6379

The ``s`` after ``redis`` turns TLS on, for every connection. Errors never
quote the URL: it may hold a password.
"""

import re
from dataclasses import dataclass, field
from typing import Final, Literal
from urllib.parse import unquote, urlsplit

SERVER_SCHEMES: Final = frozenset({"redis", "rediss"})
SENTINEL_SCHEMES: Final = frozenset({"redis+sentinel", "rediss+sentinel"})
CLUSTER_SCHEMES: Final = frozenset({"redis+cluster", "rediss+cluster"})
REDIS_SCHEMES: Final = SERVER_SCHEMES | SENTINEL_SCHEMES | CLUSTER_SCHEMES
"""Every scheme of a Redis or Valkey cache."""
REDIS_PORT: Final = 6379
SENTINEL_PORT: Final = 26379
MAX_HOSTS: Final = 32
"""Most addresses one Sentinel or Cluster URL may list."""
_HOST: Final = re.compile(
    r"(?:\[(?P<ipv6>[0-9A-Fa-f:.]+)\]|(?P<name>[A-Za-z0-9.-]{1,253}))"
    r"(?::(?P<port>[0-9]{1,5}))?"
)
_SERVICE: Final = re.compile(
    r"(?P<service>[A-Za-z0-9_.-]{1,128})(?:/(?P<db>[0-9]{1,5}))?"
)

type Kind = Literal["server", "sentinel", "cluster"]


@dataclass(frozen=True, slots=True)
class RedisTarget:
    """Where a Redis URL points and whom it logs in as; reprs hide the password."""

    kind: Kind
    tls: bool
    hosts: tuple[tuple[str, int], ...]
    """Each ``(host, port)``: the server, the Sentinels, or the Cluster's seeds."""
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    service: str | None = None
    """The name the Sentinels know the primary by (``mymaster``)."""
    db: int = 0


def parse_redis_url(url: str) -> RedisTarget:
    """Read a ``redis``, ``redis+sentinel``, or ``redis+cluster`` URL (or ``s``).

    Raises:
        ValueError: The URL is malformed; the message never quotes it.
    """
    scheme, _, rest = url.partition("://")
    scheme = scheme.lower()
    if scheme not in REDIS_SCHEMES:
        raise ValueError(
            "a Redis URL starts with redis://, redis+sentinel://, or "
            "redis+cluster:// (rediss for TLS)"
        )
    tls = scheme.startswith("rediss")
    if scheme in SERVER_SCHEMES:
        return _server(url, tls)
    if "?" in rest or "#" in rest:
        raise ValueError(
            "Sentinel and Cluster URLs take no options after ? or #; use the "
            "cache's settings"
        )
    netloc, _, path = rest.partition("/")
    userinfo, _, hostlist = netloc.rpartition("@")
    user, _, password = userinfo.partition(":")
    sentinel = scheme in SENTINEL_SCHEMES
    target = RedisTarget(
        "sentinel" if sentinel else "cluster",
        tls,
        _hosts(hostlist, SENTINEL_PORT if sentinel else REDIS_PORT),
        username=unquote(user) or None,
        password=unquote(password) or None,
    )
    if not sentinel:
        if path not in {"", "0"}:
            raise ValueError("a Cluster URL names no database: Cluster has only 0")
        return target
    found = _SERVICE.fullmatch(path)
    if found is None:
        raise ValueError(
            "a Sentinel URL names the service, then maybe a database: "
            "redis+sentinel://host:26379/mymaster/0"
        )
    return RedisTarget(
        target.kind,
        tls,
        target.hosts,
        username=target.username,
        password=target.password,
        service=found["service"],
        db=int(found["db"] or 0),
    )


def _server(url: str, tls: bool) -> RedisTarget:
    """One server's URL, read as redis-py reads it."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise ValueError(
            "the URL's port is not a number; percent-encode / ? # @ and : in "
            "the user name and password"
        ) from None
    if not parts.hostname:
        raise ValueError("a Redis URL needs a host: redis://host:6379/0")
    return RedisTarget(
        "server",
        tls,
        ((parts.hostname, port or REDIS_PORT),),
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
    )


def _hosts(hostlist: str, default_port: int) -> tuple[tuple[str, int], ...]:
    """The comma-separated ``host[:port]`` addresses of a Sentinel or Cluster URL."""
    addresses = hostlist.split(",")
    if len(addresses) > MAX_HOSTS:
        raise ValueError(f"a Redis URL lists at most {MAX_HOSTS} hosts")
    hosts = []
    for address in addresses:
        match = _HOST.fullmatch(address)
        port = int(match["port"] or default_port) if match else 0
        if match is None or not 0 < port < 65536:
            raise ValueError(
                "each host of a Sentinel or Cluster URL is host:port, "
                "[IPv6]:port, or a host alone, separated by commas"
            )
        hosts.append(((match["ipv6"] or match["name"]).lower(), port))
    return tuple(hosts)
