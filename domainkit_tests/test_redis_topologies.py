"""Redis Sentinel and Cluster: URLs, settings, clients, and failures.

No Sentinel or Cluster runs here. Clients are built and inspected without
connecting, fakeredis stands in for a Cluster's commands, and a closed port
plays one that is down. Running against real servers needs them set up
(see ``docs/cache.md``).
"""

import re
from pathlib import Path
from typing import Any, Final

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from pydantic import ValidationError
from redis.asyncio.sentinel import (
    SentinelManagedConnection,
    SentinelManagedSSLConnection,
)
from redis.exceptions import ConnectionError as RedisConnectionError

from domainkit.cache import CacheUnavailable, caches
from domainkit.cache.redis import (
    BlockingSentinelPool,
    ClusterStore,
    RedisStore,
    create_client,
    create_cluster_client,
)
from domainkit.conf import CacheConfig, Settings, override_settings
from domainkit.http.ratelimit import RedisBackend
from domainkit.utils.redis_urls import MAX_HOSTS, RedisTarget, parse_redis_url

pytestmark = pytest.mark.anyio

SENTINEL: Final = "redis+sentinel://app:s%3Acret@sentinel-1:26380,10.0.0.2,[2001:db8::7]:26381/mymaster/3"
CLUSTER: Final = "rediss+cluster://:pw@Node-1:7000,node-2"


def test_sentinel_urls_list_sentinels_service_and_database() -> None:
    """Hosts default to port 26379; credentials are decoded; the repr hides them."""
    target = parse_redis_url(SENTINEL)

    assert target == RedisTarget(
        "sentinel",
        False,
        (("sentinel-1", 26380), ("10.0.0.2", 26379), ("2001:db8::7", 26381)),
        username="app",
        password="s:cret",
        service="mymaster",
        db=3,
    )
    assert "s:cret" not in repr(target)
    assert parse_redis_url("rediss+sentinel://h/primary") == RedisTarget(
        "sentinel", True, (("h", 26379),), service="primary"
    )


def test_cluster_urls_list_seed_nodes() -> None:
    """Hosts default to port 6379, lower-cased; Cluster has only database 0."""
    assert parse_redis_url(CLUSTER) == RedisTarget(
        "cluster", True, (("node-1", 7000), ("node-2", 6379)), password="pw"
    )
    for path in ("", "/", "/0"):
        assert parse_redis_url(f"redis+cluster://h{path}").hosts == (("h", 6379),)


def test_server_urls_are_read_as_redis_py_reads_them() -> None:
    """One host, with its port, credentials, and TLS from the scheme."""
    assert parse_redis_url("REDISS://u:p%40w@Cache.Internal:6380/2") == RedisTarget(
        "server", True, (("cache.internal", 6380),), username="u", password="p@w"
    )
    assert parse_redis_url("redis://cache/0") == RedisTarget(
        "server", False, (("cache", 6379),)
    )


SCHEME: Final = (
    "a Redis URL starts with redis://, redis+sentinel://, or redis+cluster:// "
    "(rediss for TLS)"
)
NO_HOST: Final = "a Redis URL needs a host: redis://host:6379/0"
PORT: Final = (
    "the URL's port is not a number; percent-encode / ? # @ and : in the user "
    "name and password"
)
OPTIONS: Final = (
    "Sentinel and Cluster URLs take no options after ? or #; use the cache's settings"
)
SERVICE: Final = (
    "a Sentinel URL names the service, then maybe a database: "
    "redis+sentinel://host:26379/mymaster/0"
)
DATABASE: Final = "a Cluster URL names no database: Cluster has only 0"
HOST: Final = (
    "each host of a Sentinel or Cluster URL is host:port, [IPv6]:port, or a host "
    "alone, separated by commas"
)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("memcached://:s3cret@h", SCHEME),
        ("redis://:s3cret@/0", NO_HOST),
        ("redis://u:s3cret/x@h:1/0", PORT),
        ("redis+sentinel://:s3cret@h/m?ssl=0", OPTIONS),
        ("redis+cluster://:s3cret@h#x", OPTIONS),
        ("redis+sentinel://:s3cret@h", SERVICE),
        ("redis+sentinel://:s3cret@h/m/x", SERVICE),
        ("redis+sentinel://:s3cret@h/" + "m" * 129, SERVICE),
        ("redis+cluster://:s3cret@h/1", DATABASE),
        ("redis+cluster://:s3cret@", HOST),
        ("redis+cluster://:s3cret@h:0", HOST),
        ("redis+cluster://:s3cret@h:65536", HOST),
        ("redis+cluster://:s3cret@h,", HOST),
        ("redis+cluster://:s3cret@h_1", HOST),
        ("redis+cluster://:s3cret@[::1", HOST),
        (
            "redis+cluster://:s3cret@" + ",".join(["h"] * (MAX_HOSTS + 1)),
            "a Redis URL lists at most 32 hosts",
        ),
    ],
)
def test_malformed_urls_are_refused_without_quoting_them(
    url: str, message: str
) -> None:
    """Every error names the problem, never the URL, which may hold a password."""
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$") as refused:
        parse_redis_url(url)
    assert "s3cret" not in str(refused.value)


def test_the_highest_port_and_most_hosts_are_accepted() -> None:
    """Port 65535 and 32 hosts are within the limits."""
    hosts = ",".join(f"h{index}:65535" for index in range(MAX_HOSTS))
    assert len(parse_redis_url(f"redis+cluster://{hosts}").hosts) == MAX_HOSTS
    assert parse_redis_url("redis+cluster://h:1").hosts == (("h", 1),)


def test_passwords_and_options_may_hold_separators() -> None:
    """The first colon ends the user name; only the first :// ends the scheme."""
    target = parse_redis_url("redis+cluster://u:pa:ss@h")
    assert (target.username, target.password) == ("u", "pa:ss")
    assert parse_redis_url("redis://cache/0?client_name=a://b").hosts == (
        ("cache", 6379),
    )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"URL": "redis+sentinel://h/m"}, "Redis caches need a KEY_PREFIX"),
        ({"URL": "redis+cluster://h:x", "KEY_PREFIX": "x"}, "each host"),
        (
            {"URL": "redis+cluster://h", "KEY_PREFIX": "x", "TLS_CA": "ca.pem"},
            "need a rediss",
        ),
        (
            {"URL": "rediss://h/0", "KEY_PREFIX": "x", "SENTINEL_PASSWORD": "pw"},
            "SENTINEL_PASSWORD needs a redis\\+sentinel",
        ),
        ({"URL": "memory://", "SENTINEL_PASSWORD": "pw"}, "SENTINEL_PASSWORD needs"),
    ],
)
def test_sentinel_and_cluster_settings_are_checked(
    options: dict[str, object], message: str
) -> None:
    """The same rules as for one server, plus Sentinel's own password."""
    with pytest.raises(ValidationError, match=message):
        CacheConfig.model_validate(options)


def test_sentinel_and_cluster_url_properties() -> None:
    """Any host elsewhere makes a cache a network cache; ``rediss`` means TLS."""
    local = CacheConfig(URL="redis+sentinel://localhost,127.0.0.1/m", KEY_PREFIX="x")
    mixed = CacheConfig(URL="redis+cluster://localhost,node-2", KEY_PREFIX="x")
    secured = CacheConfig(
        URL="rediss+sentinel://:pw@s1/m",
        KEY_PREFIX="x",
        SENTINEL_PASSWORD="sentinel-pw",
        TLS_CA=Path("ca.pem"),
    )

    assert (local.is_network(), mixed.is_network(), secured.is_network()) == (
        False,
        True,
        True,
    )
    assert (local.uses_tls(), secured.uses_tls(), CacheConfig().uses_tls()) == (
        False,
        True,
        False,
    )
    assert (mixed.has_credentials(), secured.has_credentials()) == (False, True)
    assert CacheConfig().has_credentials() is False
    assert secured.scheme == "rediss+sentinel"
    assert "sentinel-pw" not in repr(secured)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("redis+sentinel://:pw@s1,s2/m", "network caches need rediss://"),
        ("redis+cluster://:pw@n1,n2", "network caches need rediss://"),
        ("rediss+cluster://n1,n2", "need a password or a client cert"),
    ],
)
def test_deployed_sentinels_and_clusters_need_tls_and_credentials(
    url: str, message: str
) -> None:
    """Staging and production hold them to the rules for one server."""
    base = {"ENVIRONMENT": "production", "ALLOWED_HOSTS": ["api.example.com"]}
    shared = {"URL": url, "KEY_PREFIX": "billing"}
    with pytest.raises(ValidationError, match=f"{message}.*CACHES \\['shared'\\]"):
        Settings.model_validate({**base, "CACHES": {"default": {}, "shared": shared}})

    fine = {
        "shared": {"URL": "rediss+sentinel://:pw@s1,s2/m", "KEY_PREFIX": "billing"},
        "cluster": {"URL": "rediss+cluster://:pw@n1,n2", "KEY_PREFIX": "billing"},
        "local": {"URL": "redis+cluster://localhost:7000", "KEY_PREFIX": "billing"},
    }
    deployed = Settings.model_validate({**base, "CACHES": {"default": {}, **fine}})
    assert set(deployed.CACHES) == {"default", *fine}


def test_sentinel_clients_find_the_primary_through_the_sentinels(
    tmp_path: Path,
) -> None:
    """The primary's pool waits for connections; Sentinels get the same TLS."""
    config = CacheConfig(
        URL="rediss+sentinel://app:pw@s1:26380,s2/mymaster/2",
        KEY_PREFIX="billing",
        SENTINEL_PASSWORD="sentinel-pw",
        TLS_CA=tmp_path / "ca.pem",
        TLS_CERT=tmp_path / "client.pem",
        TLS_KEY=tmp_path / "client.key",
        MAX_CONNECTIONS=7,
        CONNECT_TIMEOUT=0.25,
        SOCKET_TIMEOUT=0.5,
        RETRIES=0,
    )
    client = create_client(config)
    pool = client.connection_pool
    assert isinstance(pool, BlockingSentinelPool)
    options = pool.connection_kwargs
    tls = {
        "ssl_ca_certs": str(tmp_path / "ca.pem"),
        "ssl_certfile": str(tmp_path / "client.pem"),
        "ssl_keyfile": str(tmp_path / "client.key"),
    }

    assert (pool.service_name, pool.max_connections, pool.timeout) == (
        "mymaster",
        7,
        0.25,
    )
    assert pool.connection_class is SentinelManagedSSLConnection
    assert (options["username"], options["password"], options["db"]) == ("app", "pw", 2)
    assert (options["socket_timeout"], options["socket_connect_timeout"]) == (0.5, 0.25)
    assert options["retry"].get_retries() == 0
    assert tls.items() <= options.items()
    sentinels = pool.sentinel_manager.sentinels
    assert [
        (
            s.connection_pool.connection_kwargs["host"],
            s.connection_pool.connection_kwargs["port"],
        )
        for s in sentinels
    ] == [("s1", 26380), ("s2", 26379)]
    sentinel_options = sentinels[0].connection_pool.connection_kwargs
    assert sentinel_options["password"] == "sentinel-pw"
    assert sentinel_options["retry"].get_retries() == 0
    assert (
        sentinel_options["socket_timeout"],
        sentinel_options["socket_connect_timeout"],
    ) == (
        0.5,
        0.25,
    )
    assert tls.items() <= sentinel_options.items()


def test_sentinel_clients_without_tls_or_a_sentinel_password() -> None:
    """Plain connections, and Sentinels that ask for no password."""
    plain = create_client(CacheConfig(URL="redis+sentinel://s1/m", KEY_PREFIX="x"))
    system_cas = create_client(
        CacheConfig(URL="rediss+sentinel://s1/m", KEY_PREFIX="x")
    )
    pool = plain.connection_pool
    assert isinstance(pool, BlockingSentinelPool)

    assert pool.connection_class is SentinelManagedConnection
    assert "ssl_ca_certs" not in pool.connection_kwargs
    sentinel = pool.sentinel_manager.sentinels[0].connection_pool.connection_kwargs
    assert sentinel.get("password") is None
    assert system_cas.connection_pool.connection_class is SentinelManagedSSLConnection
    assert "ssl_ca_certs" not in system_cas.connection_pool.connection_kwargs


async def test_a_busy_sentinel_pool_waits_then_gives_up() -> None:
    """Every connection in use: a command waits ``CONNECT_TIMEOUT``, then fails."""
    config = CacheConfig(
        URL="redis+sentinel://s1/m",
        KEY_PREFIX="x",
        MAX_CONNECTIONS=1,
        CONNECT_TIMEOUT=0.05,
    )
    client = create_client(config)
    pool = client.connection_pool
    pool._in_use_connections.add(pool.make_connection())  # type: ignore[no-untyped-call]
    try:
        with pytest.raises(RedisConnectionError, match="No connection available"):
            await client.ping()
    finally:
        pool._in_use_connections.clear()
        await RedisStore(client).close()


def test_cluster_clients_start_from_the_seed_nodes(tmp_path: Path) -> None:
    """The client knows its seeds and options; nothing connects yet."""
    config = CacheConfig(
        URL="rediss+cluster://app:pw@n1:7000,n2",
        KEY_PREFIX="billing",
        TLS_CA=tmp_path / "ca.pem",
        MAX_CONNECTIONS=9,
        RETRIES=2,
    )
    client = create_cluster_client(config)
    options = client.connection_kwargs

    assert [(node.host, node.port) for node in client.startup_nodes] == [
        ("n1", 7000),
        ("n2", 6379),
    ]
    assert (options["username"], options["password"]) == ("app", "pw")
    assert options["ssl_ca_certs"] == str(tmp_path / "ca.pem")
    assert (options["socket_timeout"], options["socket_connect_timeout"]) == (1.0, 1.0)
    assert options["max_connections"] == 9
    assert client.retry.get_retries() == 2
    assert isinstance(RedisStore.from_config(config), ClusterStore)
    plain = CacheConfig(URL="redis+cluster://n1", KEY_PREFIX="x")
    assert "ssl_ca_certs" not in create_cluster_client(plain).connection_kwargs
    with pytest.raises(ValueError, match="create_cluster_client"):
        create_client(plain)


class FakeCluster(FakeAsyncRedis):
    """fakeredis, with the Cluster client's slot-splitting ``MGET``."""

    split_reads: list[list[str]]

    async def mget_nonatomic(self, keys: list[str]) -> list[Any]:
        """Read as one ``MGET`` per slot would: here, all at once."""
        self.split_reads.append(keys)
        return await self.mget(keys)


async def test_cluster_stores_never_cross_slots_in_one_command() -> None:
    """Reads of several keys go through the slot-splitting ``MGET``."""
    client = FakeCluster(server=FakeServer())
    client.split_reads = []
    store = ClusterStore(client)
    try:
        await store.set_many({"a": b"1", "b": b"2"}, 1000)
        assert await store.get_many(["a", "b", "missing"]) == [b"1", b"2", None]
        assert await store.incr("n", 2, None) == 2
        assert await store.clear("") == 3
    finally:
        await store.close()

    assert client.split_reads == [["a", "b", "missing"]]


async def test_closing_a_sentinel_store_closes_the_sentinels_too() -> None:
    """The primary's client and every Sentinel's connections are released."""
    client = create_client(CacheConfig(URL="redis+sentinel://s1,s2/m", KEY_PREFIX="x"))
    closed: list[str] = []
    manager = client.connection_pool.sentinel_manager  # type: ignore[attr-defined]

    async def close_sentinels() -> None:
        closed.append("sentinels")

    manager.aclose = close_sentinels
    await RedisStore(client).close()

    assert closed == ["sentinels"]


@pytest.mark.parametrize(
    "url", ["redis+cluster://127.0.0.1:9", "redis+sentinel://127.0.0.1:9/m"]
)
async def test_sentinels_and_clusters_that_are_down_fail_open(url: str) -> None:
    """Unreachable: ``get_or_set`` computes, plain calls raise ``CacheUnavailable``."""
    down = {"URL": url, "KEY_PREFIX": "down", "CONNECT_TIMEOUT": 0.5, "RETRIES": 0}
    with override_settings(CACHES={"default": down}):
        cache = caches["default"]

        async def compute() -> int:
            return 5

        try:
            assert isinstance(cache.store, RedisStore)
            assert await cache.get_or_set("k", compute) == 5
            with pytest.raises(CacheUnavailable):
                await cache.check()
        finally:
            await caches.close_all()


async def test_rate_limits_count_in_sentinels_and_clusters() -> None:
    """``RedisBackend`` takes any Redis cache; one that is down lets requests pass."""
    down = {"URL": "redis+cluster://127.0.0.1:9", "KEY_PREFIX": "down", "RETRIES": 0}
    with override_settings(
        CACHES={"default": {}, "limits": down}, RATE_LIMIT_CACHE="limits"
    ):
        try:
            decision = await RedisBackend().hit("ip:203.0.113.9", 10, 60)
        finally:
            await caches.close_all()

    assert (decision.allowed, decision.remaining) == (True, 9)
