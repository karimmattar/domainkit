"""Each request's work is done once, with the answers it always had.

Client addresses are parsed once per request, and remembered across
requests; security headers are appended raw; request ids cost no UUID;
metric handles are looked up once per series. Every cached answer is
checked against the work done afresh, and every cost is counted, never
timed.
"""

import asyncio
import ipaddress
import re
from collections import Counter
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from prometheus_client import REGISTRY
from prometheus_client.metrics import MetricWrapperBase
from starlette.datastructures import MutableHeaders
from starlette.types import Message, Receive, Scope, Send

from domainkit.conf import override_settings, settings
from domainkit.http import get_asgi_application, metrics, middleware, ratelimit
from domainkit.http.metrics import IN_PROGRESS, REQUESTS, MetricsMiddleware
from domainkit.http.middleware import (
    ADDRESS_CACHE,
    MAX_CACHED_ADDRESS,
    ProxyHeadersMiddleware,
    RawHeaders,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    _ip,
    add_missing_headers,
    new_request_id,
    request_headers,
    set_headers,
)
from domainkit.http.ratelimit import ADDRESS, address_key, client_group
from domainkit_tests.conftest import UseAuth

ZONE = "fe80::1%" + "z" * (MAX_CACHED_ADDRESS - len("fe80::1%"))
"""A scoped IPv6 address as long as an address that is still remembered."""
HOSTS = [
    "203.0.113.7",
    "0.0.0.0",  # noqa: S104 - an address to parse, not one to bind.
    "255.255.255.255",
    "2001:db8:1:2:aaaa::1",
    "2001:DB8:1:2::9",
    "::",
    "::1",
    "::ffff:203.0.113.7",
    "::ffff:0:0",
    "fe80::1%eth0",
    ZONE,
    ZONE + "z",  # Too long to remember.
    "1.2.3.4%z",
    "1.2.3.04",
    "[2001:db8::1]",
    "2001:db8::1:",
    "testclient",
    "",
    "not an address",
]
PROXIES = ["10.0.0.0/8", "2001:db8:aa::/48"]


def parsed_group(host: str) -> str:
    """``client_group`` as 0.8.0 worked it out: parsing every time."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        prefix = settings.RATE_LIMIT_IPV6_PREFIX
        return str(ipaddress.IPv6Interface((address, prefix)).network)
    return str(address)


IPV6 = {"2001:db8:1:2:aaaa::1", "2001:DB8:1:2::9", "::", "::1", "fe80::1%eth0"}
IPV6 |= {ZONE, ZONE + "z"}
"""The hosts of ``HOSTS`` grouped by network, the others by address."""

type Work = tuple[object, ...]


def forget() -> None:
    """Remember no client group."""
    ratelimit._cached_address_group.cache_clear()
    ratelimit._cached_network.cache_clear()


@pytest.fixture(autouse=True)
def forget_groups() -> Iterator[None]:
    """Each test starts with no client group remembered."""
    forget()
    yield
    forget()


@pytest.fixture
def parses(monkeypatch: pytest.MonkeyPatch) -> list[Work]:
    """What ``client_group`` worked out, in order: a host, or an IPv6 network."""
    parsed: list[Work] = []
    address_group, network = ratelimit._address_group, ratelimit._network

    def host(text: str) -> str | None:
        parsed.append(("host", text))
        return address_group(text)

    def within(text: str, prefix: int) -> str:
        parsed.append(("network", text, prefix))
        return network(text, prefix)

    monkeypatch.setattr(ratelimit, "_address_group", host)
    monkeypatch.setattr(ratelimit, "_network", within)
    return parsed


@pytest.mark.parametrize("prefix", [64, 56, 128, 1])
def test_client_groups_are_remembered_as_parsed(
    prefix: int, parses: list[Work]
) -> None:
    """IPv4, IPv6, mapped, scoped, malformed: remembered or not, the same group."""
    with override_settings(RATE_LIMIT_IPV6_PREFIX=prefix):
        for host in HOSTS:
            assert client_group(host) == client_group(host) == parsed_group(host)

    # Worked out once, but for the host too long to keep: each time.
    expected: Counter[Work] = Counter()
    for host in HOSTS:
        times = 1 if len(host) <= MAX_CACHED_ADDRESS else 2
        expected["host", host] += times
        if host in IPV6:
            expected["network", host, prefix] += times
    assert Counter(parses) == expected
    assert ratelimit._cached_address_group.cache_info().currsize == len(HOSTS) - 1
    assert ratelimit._cached_network.cache_info().currsize == len(IPV6) - 1


def test_ipv4_clients_read_no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an IPv6 host needs ``RATE_LIMIT_IPV6_PREFIX`` (N2)."""

    class Unread:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"settings.{name} was read")

    monkeypatch.setattr(ratelimit, "settings", Unread())

    for host in ("203.0.113.7", "::ffff:203.0.113.7", "testclient", ZONE[:-1] + "%"):
        assert client_group(host) == host.removeprefix("::ffff:")
    with pytest.raises(AssertionError, match="RATE_LIMIT_IPV6_PREFIX"):
        client_group("2001:db8::1")


@given(st.ip_addresses().map(str) | st.text(max_size=MAX_CACHED_ADDRESS + 8))
def test_any_host_gets_the_parsed_group(host: str) -> None:
    """Whatever the host, the remembered group is the parsed one."""
    assert client_group(host) == client_group(host) == parsed_group(host)


def test_a_new_prefix_is_never_answered_from_memory(parses: list[Work]) -> None:
    """Networks are remembered by host and ``RATE_LIMIT_IPV6_PREFIX``."""
    host = "2001:db8:1:2ff::1"
    with override_settings(RATE_LIMIT_IPV6_PREFIX=56):
        narrow = client_group(host)
    wide = client_group(host)
    with override_settings(RATE_LIMIT_IPV6_PREFIX=56):
        assert client_group(host) == narrow == "2001:db8:1:200::/56"

    assert wide == "2001:db8:1:2ff::/64"
    assert parses == [("host", host), ("network", host, 56), ("network", host, 64)]


def test_memory_of_hosts_is_bounded() -> None:
    """At most ``ADDRESS_CACHE`` hosts each, the latest used; none longer than 64."""
    caches = (ratelimit._cached_address_group, ratelimit._cached_network)

    assert (ADDRESS_CACHE, MAX_CACHED_ADDRESS) == (4096, 64)
    assert [cache.cache_info().maxsize for cache in caches] == [ADDRESS_CACHE] * 2
    for number in range(ADDRESS_CACHE + 10):
        client_group(str(ipaddress.IPv4Address(number)))
        client_group(str(ipaddress.IPv6Address(number << 64)))
    client_group(ZONE + "z")
    assert [cache.cache_info().currsize for cache in caches] == [ADDRESS_CACHE] * 2


def test_the_address_is_worked_out_once_per_request(parses: list[Work]) -> None:
    """The scope keeps the key with the host it is for (``ADDRESS``)."""
    scope: Scope = {"client": ("2001:db8::7", 1)}

    keys = [address_key(scope) for _ in range(3)]

    assert keys == ["ip:2001:db8::/64"] * 3
    assert ADDRESS == "domainkit.address"  # Namespaced, as docs/performance.md says.
    assert scope[ADDRESS] == ("2001:db8::7", "ip:2001:db8::/64")
    assert parses == [("host", "2001:db8::7"), ("network", "2001:db8::7", 64)]
    assert address_key({}) == "ip:unknown"


def test_a_key_kept_for_another_host_is_worked_out_again() -> None:
    """A key from before the proxy's headers were read is not the client's."""
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(address_key(scope))

    with override_settings(TRUSTED_PROXIES=PROXIES):
        proxy = ProxyHeadersMiddleware(app)
    scope: Scope = {
        "type": "http",
        "client": ("10.0.0.5", 1234),
        "headers": [(b"x-forwarded-for", b"203.0.113.9")],
    }
    seen.append(address_key(scope))
    asyncio.run(proxy(scope, _nothing, _ignore))
    forged: Scope = {"client": ("203.0.113.9", 1), ADDRESS: ("198.51.100.1", "ip:x")}

    assert seen == ["ip:10.0.0.5", "ip:203.0.113.9"]
    assert address_key(forged) == "ip:203.0.113.9"
    assert forged[ADDRESS] == ("203.0.113.9", "ip:203.0.113.9")


def test_a_client_cannot_choose_its_key(use_auth: UseAuth) -> None:
    """Headers never reach the scope's own keys; counts stay per client."""
    use_auth(
        settings={
            "RATE_LIMIT": "2/minute",
            "MIDDLEWARE": [
                "domainkit.http.middleware.RequestContextMiddleware",
                "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
                "domainkit.http.ratelimit.RateLimitMiddleware",
            ],
        }
    )
    app = _application()
    forged = {ADDRESS: "ip:198.51.100.1", "X-Forwarded-For": "198.51.100.1"}
    first = TestClient(app, client=("203.0.113.1", 1))
    other = TestClient(app, client=("203.0.113.2", 1))

    remaining = [
        first.get("/ok", headers=forged).headers["RateLimit-Remaining"],
        first.get("/ok", headers=forged).headers["RateLimit-Remaining"],
        other.get("/ok", headers=forged).headers["RateLimit-Remaining"],
    ]

    assert remaining == ["1", "0", "1"]


def test_authentication_and_the_limit_share_one_parse(
    use_auth: UseAuth, parses: list[Work], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold check and the rate limit read one key; a known client, none."""
    groups: list[str] = []
    group = ratelimit.client_group

    def counted(host: str) -> str:
        groups.append(host)
        return group(host)

    monkeypatch.setattr(ratelimit, "client_group", counted)
    use_auth(
        settings={
            "RATE_LIMIT": "100/minute",
            "MIDDLEWARE": [
                "domainkit.http.middleware.RequestContextMiddleware",
                "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
                "domainkit.http.ratelimit.RateLimitMiddleware",
            ],
        }
    )
    client = TestClient(_application(), client=("2001:db8::9", 1))

    statuses = [client.get("/ok").status_code for _ in range(3)]

    assert statuses == [200, 200, 200]
    assert groups == ["2001:db8::9"] * 3  # Once per request, not twice.
    assert parses == [("host", "2001:db8::9"), ("network", "2001:db8::9", 64)]


@pytest.mark.parametrize(
    "text",
    [
        "10.0.0.5",
        "10.0.0.5:8080",
        "203.0.113.9",
        "203.0.113.9:4711",
        "::ffff:10.0.0.5",
        "[::ffff:10.0.0.5]:443",
        "2001:db8:aa::1",
        "[2001:db8:aa::1]:443",
        "2001:db8:bb::1",
        "fe80::1%eth0",
        "[fe80::1%eth0]:80",
        ZONE,
        ZONE + "z",
        "[2001:db8::1",
        "[2001:db8::1]443",
        "203.0.113.9:",
        "203.0.113.9:-1",
        "testclient",
        "",
    ],
)
def test_proxy_addresses_are_remembered_as_parsed(text: str) -> None:
    """The proxy's trust and each hop: remembered, as parsing found them."""
    with override_settings(TRUSTED_PROXIES=PROXIES):
        proxy = ProxyHeadersMiddleware(_nothing_app)
    ip = _ip(text)
    expected = None if ip is None else (str(ip), any(ip in n for n in proxy.networks))

    assert proxy.address(text) == proxy.address(text) == expected
    assert proxy.trusted(text) is (expected is not None and expected[1])
    remembered = proxy._addresses.cache_info().currsize
    assert remembered == int(len(text) <= MAX_CACHED_ADDRESS)


@given(st.ip_addresses().map(str) | st.text(max_size=MAX_CACHED_ADDRESS + 8))
def test_any_hop_is_trusted_as_parsed(text: str) -> None:
    """Whatever a header says, the remembered answer is the parsed one."""
    with override_settings(TRUSTED_PROXIES=PROXIES):
        proxy = ProxyHeadersMiddleware(_nothing_app)
    ip = _ip(text)
    expected = None if ip is None else (str(ip), any(ip in n for n in proxy.networks))

    assert proxy.address(text) == proxy.address(text) == expected


def test_each_proxy_middleware_trusts_its_own_networks() -> None:
    """Remembered answers belong to one middleware, with its networks."""
    with override_settings(TRUSTED_PROXIES=PROXIES):
        trusting = ProxyHeadersMiddleware(_nothing_app)
    with override_settings(TRUSTED_PROXIES=[]):
        wary = ProxyHeadersMiddleware(_nothing_app)

    assert trusting.trusted("10.0.0.5")
    assert not wary.trusted("10.0.0.5")
    info = trusting._addresses.cache_info()
    assert info.maxsize == ADDRESS_CACHE


NAMES = [
    b"cache-control",
    b"Cache-Control",
    b"x-frame-options",
    b"x-request-id",
    b"content-type",
    b"referrer-policy",
    b"strict-transport-security",
    b"ratelimit-limit",
]
RAW = st.lists(st.tuples(st.sampled_from(NAMES), st.binary(max_size=4)), max_size=8)


def checked_one_by_one(
    headers: RawHeaders, defaults: list[tuple[str, str]]
) -> RawHeaders:
    """What 0.8.0 did: check a ``MutableHeaders`` for each, set the missing."""
    message: Message = {"headers": headers}
    found = MutableHeaders(scope=message)
    for name, value in defaults:
        if name not in found:
            found[name] = value
    sent: RawHeaders = message["headers"]
    return sent


def set_one_by_one(headers: RawHeaders, items: list[tuple[str, str]]) -> RawHeaders:
    """What 0.8.0 did: set each through a ``MutableHeaders``."""
    message: Message = {"headers": headers}
    MutableHeaders(scope=message).update(dict(items))
    sent: RawHeaders = message["headers"]
    return sent


DEFAULTS = [
    ("Cache-Control", "no-store"),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("Strict-Transport-Security", "max-age=60; includeSubDomains"),
]


def encoded(headers: list[tuple[str, str]]) -> RawHeaders:
    """``headers`` as a response start carries them."""
    return [(name.lower().encode(), value.encode()) for name, value in headers]


@given(RAW, st.lists(st.sampled_from(DEFAULTS), unique=True))
def test_missing_headers_are_added_as_one_by_one(
    headers: RawHeaders, defaults: list[tuple[str, str]]
) -> None:
    """The same headers, values, and order, and the input left as it was."""
    before = list(headers)

    added = add_missing_headers(headers, encoded(defaults))

    assert added == checked_one_by_one(list(headers), defaults)
    assert headers == before
    assert added is not headers


@given(
    RAW,
    st.lists(
        st.tuples(
            st.sampled_from(["X-Request-ID", "RateLimit-Limit", "Cache-Control"]),
            st.sampled_from(["a", "b"]),
        ),
        unique_by=lambda item: item[0],
    ),
)
def test_headers_are_set_as_one_by_one(
    headers: RawHeaders, items: list[tuple[str, str]]
) -> None:
    """Replaced in place, repeats dropped, new ones last: as ``MutableHeaders``."""
    before = list(headers)

    result = set_headers(headers, encoded(items))

    assert result == set_one_by_one(list(headers), items)
    assert headers == before


def test_security_headers_are_appended_once_and_never_overwritten() -> None:
    """One append, in order; what the application set stays; its list is its own."""
    own: RawHeaders = [(b"x-frame-options", b"SAMEORIGIN"), (b"content-length", b"0")]
    starts: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": own})

    async def send(message: Message) -> None:
        starts.append(message)

    with override_settings(SECURE_HSTS_SECONDS=60, SECURE_REFERRER_POLICY="origin"):
        secured = SecurityHeadersMiddleware(app)
    for _ in range(2):
        asyncio.run(secured({"type": "http"}, _nothing, send))

    assert [start["headers"] for start in starts] == [
        [
            (b"x-frame-options", b"SAMEORIGIN"),
            (b"content-length", b"0"),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"origin"),
            (b"strict-transport-security", b"max-age=60"),
        ]
    ] * 2
    assert own == [(b"x-frame-options", b"SAMEORIGIN"), (b"content-length", b"0")]


def test_request_headers_become_a_list_once() -> None:
    """ASGI allows any iterable; it is read as a list, as ``Headers`` leaves it."""
    scope: Scope = {"headers": iter([(b"x-request-id", b"abc"), (b"host", b"h")])}

    assert request_headers(scope) == [(b"x-request-id", b"abc"), (b"host", b"h")]
    assert middleware._header(scope, b"host") == "h"
    assert middleware._header({"headers": ((b"host", b"t"),)}, b"host") == "t"
    assert middleware._header(scope, b"x-request-id") == "abc"
    assert middleware._header(scope, b"missing") is None
    # Read straight from a one-shot iterable, a header after another is found.
    once: Scope = {"headers": iter([(b"host", b"h"), (b"x-request-id", b"abc")])}
    assert middleware._header(once, b"x-request-id") == "abc"
    assert middleware._header(once, b"host") == "h"
    assert once["headers"] == [(b"host", b"h"), (b"x-request-id", b"abc")]


def test_request_ids_are_32_hex_digits_and_new_each_time() -> None:
    """The format of ``uuid4().hex``: logs, audit rows, and clients keep reading it."""
    ids = {new_request_id() for _ in range(1000)}

    assert len(ids) == 1000
    assert all(re.fullmatch(r"[0-9a-f]{32}", request_id) for request_id in ids)


@pytest.mark.parametrize(
    ("supplied", "kept"),
    [
        ("abc-123", True),
        ("A" * 128, True),
        ("A" * 129, False),
        ("a.b:c_d-e", True),
        ("-starts-badly", False),
        ("bad id", False),
        ("caf\xe9", False),
        ("", False),
    ],
)
def test_incoming_request_ids_are_accepted_as_before(supplied: str, kept: bool) -> None:
    """A valid ``X-Request-ID`` is echoed; anything else gets a new id."""
    starts: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        headers = [(b"x-request-id", b"mine"), (b"x-request-id", b"again")]
        await send({"type": "http.response.start", "status": 204, "headers": headers})

    async def send(message: Message) -> None:
        starts.append(message)

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-request-id", supplied.encode("latin-1"))],
    }
    asyncio.run(RequestContextMiddleware(app)(scope, _nothing, send))

    echoed = [value for name, value in starts[0]["headers"] if name == b"x-request-id"]
    assert len(echoed) == 1
    if kept:
        assert echoed == [supplied.encode()]
    else:
        assert re.fullmatch(rb"[0-9a-f]{32}", echoed[0])


@pytest.fixture
def labelled(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Each ``labels()`` lookup of the request metrics, in order."""
    looked_up: list[tuple[str, ...]] = []
    measured: tuple[MetricWrapperBase, ...] = (REQUESTS, metrics.DURATION, IN_PROGRESS)
    for metric in measured:

        def counted(*values: str, labels: Callable[..., Any] = metric.labels) -> Any:  # noqa: ANN401 - a metric.
            looked_up.append(values)
            return labels(*values)

        monkeypatch.setattr(metric, "labels", counted)
    return looked_up


def test_metric_handles_are_looked_up_once_per_series(
    labelled: list[tuple[str, ...]],
) -> None:
    """The same series and values; ``labels()`` only for a series not seen yet."""
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> Response:
        return Response(status_code=204)

    client = TestClient(MetricsMiddleware(app))
    labels = {"method": "GET", "route": "/items/{item_id}", "status": "204"}
    count = REGISTRY.get_sample_value("http_requests_total", labels) or 0.0

    for number in range(3):
        client.get(f"/items/{number}")
    client.post("/items/1")

    assert REGISTRY.get_sample_value("http_requests_total", labels) == count + 3
    assert labelled == [
        ("GET",),
        ("GET", "/items/{item_id}", "204"),
        ("GET", "/items/{item_id}"),
        ("POST",),
        ("POST", "/items/{item_id}", "405"),
        ("POST", "/items/{item_id}"),
    ]


def test_metric_handles_kept_are_bounded(
    labelled: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past ``MAX_SERIES``, a series is looked up for each request, and counted."""
    assert metrics.MAX_SERIES == 4096  # As docs/reference.md says.
    monkeypatch.setattr(metrics, "MAX_SERIES", 1)
    counter = MetricsMiddleware(_nothing_app)

    first = [counter.series("GET", "/a", 200) for _ in range(2)]
    second = [counter.series("GET", "/b", 200) for _ in range(2)]

    assert first[0] is first[1]
    assert second[0] == second[1]
    assert list(counter._series) == [("GET", "/a", 200)]
    assert (
        labelled
        == [("GET", "/a", "200"), ("GET", "/a")]
        + [
            ("GET", "/b", "200"),
            ("GET", "/b"),
        ]
        * 2
    )


def _application() -> FastAPI:
    """The tests' application, built now."""
    return get_asgi_application()


async def _nothing() -> Message:
    """A client that says nothing more."""
    return {"type": "http.disconnect"}


async def _ignore(message: Message) -> None:
    """A server that takes any message."""


async def _nothing_app(scope: Scope, receive: Receive, send: Send) -> None:
    """An application that answers nothing."""
