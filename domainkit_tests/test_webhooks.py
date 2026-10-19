"""Webhooks: Standard Webhooks signatures, the SSRF guard, and the HTTP sender."""

import asyncio
import ipaddress
import socket
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from domainkit.contrib.webhooks import client, guard, signing
from domainkit.contrib.webhooks.guard import (
    UnsafeTarget,
    checked_address,
    checked_url,
    refusal,
)
from domainkit.contrib.webhooks.models import (
    InvalidEventTypes,
    Subscription,
    SubscriptionChanged,
    SubscriptionCreated,
    SubscriptionDeleted,
    SubscriptionSecretRotated,
    checked_event_types,
)
from domainkit.contrib.webhooks.settings import WebhookSettings
from domainkit.contrib.webhooks.signing import InvalidSignature, sign, verify
from domainkit.ddd import BusinessRuleViolation

pytestmark = pytest.mark.anyio

# The example of the Standard Webhooks specification.
SPEC_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
SPEC_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
SPEC_TIME = 1614265330
SPEC_BODY = b'{"test": 2432232314}'
SPEC_SIGNATURE = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
LOCAL = (ipaddress.ip_network("127.0.0.0/8"),)


# Signatures ---------------------------------------------------------------------


def test_signatures_match_the_specification() -> None:
    """HMAC-SHA256 over id, timestamp, and body, keyed with the secret's bytes."""
    assert sign(SPEC_SECRET, SPEC_ID, SPEC_TIME, SPEC_BODY) == SPEC_SIGNATURE
    bare = SPEC_SECRET.removeprefix("whsec_")
    assert sign(bare, SPEC_ID, SPEC_TIME, SPEC_BODY) == SPEC_SIGNATURE
    assert sign(SPEC_SECRET, SPEC_ID, SPEC_TIME + 1, SPEC_BODY) != SPEC_SIGNATURE
    assert sign(SPEC_SECRET, "msg_other", SPEC_TIME, SPEC_BODY) != SPEC_SIGNATURE
    assert sign(SPEC_SECRET, SPEC_ID, SPEC_TIME, SPEC_BODY + b" ") != SPEC_SIGNATURE


def test_secrets_are_random_and_well_formed() -> None:
    """whsec_ and 32 random bytes in base64."""
    first, second = signing.new_secret(), signing.new_secret()

    assert first != second
    assert first.startswith("whsec_")
    assert len(signing.secret_key(first)) == 32
    for bad in ("whsec_not base64!", "whsec_", "whsec_YQ", ""):
        with pytest.raises(
            ValueError, match=r"^A webhook secret is 'whsec_' and base64\.$"
        ):
            signing.secret_key(bad)


def test_headers_carry_one_signature_per_secret() -> None:
    """Rotating secrets sign side by side, separated by a space."""
    other = signing.new_secret()
    headers = signing.signed_headers(
        [SPEC_SECRET, other], SPEC_ID, SPEC_TIME, SPEC_BODY
    )

    assert headers == {
        "webhook-id": SPEC_ID,
        "webhook-timestamp": str(SPEC_TIME),
        "webhook-signature": (
            f"{SPEC_SIGNATURE} {sign(other, SPEC_ID, SPEC_TIME, SPEC_BODY)}"
        ),
    }


def test_receivers_can_verify_deliveries() -> None:
    """verify() accepts a fresh delivery signed with the secret."""
    headers = signing.signed_headers([SPEC_SECRET], SPEC_ID, SPEC_TIME, SPEC_BODY)
    shouting = {name.upper(): value for name, value in headers.items()}
    two = {**headers, "webhook-signature": f"v1,bogus {SPEC_SIGNATURE}"}

    assert verify(SPEC_SECRET, headers, SPEC_BODY, now=SPEC_TIME) == SPEC_ID
    assert verify(SPEC_SECRET, shouting, SPEC_BODY, now=SPEC_TIME + 300) == SPEC_ID
    assert verify(SPEC_SECRET, two, SPEC_BODY, now=SPEC_TIME - 300) == SPEC_ID


@pytest.mark.parametrize(
    ("change", "now", "error"),
    [
        ({"webhook-id": ""}, SPEC_TIME, "missing or malformed"),
        ({"webhook-timestamp": "soon"}, SPEC_TIME, "missing or malformed"),
        ({"webhook-timestamp": "²"}, SPEC_TIME, "missing or malformed"),
        ({"webhook-timestamp": "1" * 13}, SPEC_TIME, "missing or malformed"),
        ({"webhook-signature": ""}, SPEC_TIME, "missing or malformed"),
        ({}, SPEC_TIME + 301, "too old or too new"),
        ({}, SPEC_TIME - 301, "too old or too new"),
        ({"webhook-signature": "v1,bogus"}, SPEC_TIME, "No webhook signature matches"),
        ({"webhook-signature": SPEC_SIGNATURE[3:]}, SPEC_TIME, "No webhook signature"),
        ({"webhook-signature": "v2," + SPEC_SIGNATURE[3:]}, SPEC_TIME, "No webhook"),
        ({"webhook-id": "msg_other"}, SPEC_TIME, "No webhook signature matches"),
        (
            {"webhook-signature": " ".join(["v1,x"] * 10 + [SPEC_SIGNATURE])},
            SPEC_TIME,
            "No webhook signature matches",  # Only the first ten are looked at.
        ),
    ],
)
def test_receivers_refuse_bad_deliveries(
    change: dict[str, str], now: int, error: str
) -> None:
    """Missing headers, stale times, and wrong signatures are refused."""
    headers = {**signing.signed_headers([SPEC_SECRET], SPEC_ID, SPEC_TIME, SPEC_BODY)}
    headers.update(change)

    with pytest.raises(InvalidSignature, match=error):
        verify(SPEC_SECRET, headers, SPEC_BODY, now=now)
    with pytest.raises(InvalidSignature):
        verify(signing.new_secret(), {**headers}, SPEC_BODY)


def test_verify_uses_the_clock_by_default() -> None:
    """Without now=, the current time decides."""
    stamp = int(datetime.now(UTC).timestamp())
    headers = signing.signed_headers([SPEC_SECRET], SPEC_ID, stamp, SPEC_BODY)

    assert verify(SPEC_SECRET, headers, SPEC_BODY) == SPEC_ID
    with pytest.raises(InvalidSignature, match="No webhook signature matches"):
        verify(SPEC_SECRET, headers, b"tampered")


# Subscriptions ------------------------------------------------------------------


def test_subscriptions_record_their_changes_without_secrets() -> None:
    """Events name the host and the fields changed; reprs hide secrets."""
    subscription = Subscription.create(
        "https://hooks.example.com/t/SECRET-TOKEN", "hooks.example.com", ["a", "a", "b"]
    )
    created = subscription.pull_events()
    secret = subscription.secret

    assert subscription.event_types == ["a", "b"]
    assert created == [
        SubscriptionCreated(
            subscription_id=subscription.id,
            host="hooks.example.com",
            event_types=("a", "b"),
            event_id=created[0].event_id,
            occurred_at=created[0].occurred_at,
        )
    ]
    assert secret not in repr(subscription)
    assert "SECRET-TOKEN" not in repr(created)

    subscription.change(url=subscription.url, event_types=["b", "a"], active=True)
    subscription.change(description="", event_types=["a", "b"])
    assert subscription.pull_events() == []
    subscription.change(
        url="https://other.example.com/",
        event_types=["*"],
        active=False,
        description="d",
    )
    (changed,) = subscription.pull_events()
    assert isinstance(changed, SubscriptionChanged)
    assert changed.fields == ("url", "event_types", "active", "description")

    now = datetime.now(UTC)
    new = subscription.rotate_secret(timedelta(hours=1))
    assert (new, subscription.previous_secret) == (subscription.secret, secret)
    assert subscription.signing_secrets(now) == [new, secret]
    assert subscription.signing_secrets(now + timedelta(hours=2)) == [new]
    newest = subscription.rotate_secret(timedelta(0))
    assert subscription.signing_secrets(now) == [newest]
    assert subscription.previous_secret is None
    subscription.delete()
    rotated, _, deleted = subscription.pull_events()
    assert isinstance(rotated, SubscriptionSecretRotated)
    assert isinstance(deleted, SubscriptionDeleted)
    assert new not in repr(subscription)


def test_subscriptions_want_their_event_types() -> None:
    """A type it names, or any with '*', while active."""
    named = Subscription.create("https://a.example/", "a.example", ["invoice.paid"])
    everything = Subscription.create(
        "https://a.example/", "a.example", ["*"], active=False
    )

    assert named.wants("invoice.paid")
    assert not named.wants("invoice.sent")
    assert not everything.wants("invoice.paid")
    everything.change(active=True)
    assert everything.wants("anything")


@pytest.mark.parametrize(
    "event_types",
    [
        [],
        ["x"] * 0,
        [f"t{i}" for i in range(51)],
        ["bad type"],
        ["-x"],
        ["a" * 129],
        [""],
    ],
)
def test_event_types_are_checked(event_types: list[str]) -> None:
    """One to fifty, each '*' or a name of up to 128 characters."""
    with pytest.raises(InvalidEventTypes):
        checked_event_types(event_types)


def test_good_event_types_pass() -> None:
    """Names with dots, colons, dashes, and underscores; '*'."""
    names = ["invoice.paid", "a:b-c_d", "*", "A" * 128, *[f"t{i}" for i in range(46)]]
    assert checked_event_types(names) == names
    with pytest.raises(BusinessRuleViolation, match="at most 255 characters"):
        Subscription.create("https://a.example/", "a.example", ["x"], "d" * 256)


# The SSRF guard -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("93.184.215.14", None),
        ("2606:4700::1111", None),
        ("::ffff:8.8.8.8", None),
        ("64:ff9b::808:808", None),  # NAT64 of a public address.
        ("10.0.0.1", "private"),
        ("172.16.5.4", "private"),
        ("192.168.1.1", "private"),
        ("127.0.0.1", "private"),
        ("127.255.255.254", "private"),
        ("0.0.0.0", "unspecified"),  # noqa: S104 - an address, not a binding.
        ("0.1.2.3", "private"),
        ("100.64.0.1", "private"),  # Shared address space (CGNAT).
        ("169.254.10.10", "private"),
        ("192.0.2.1", "private"),  # Documentation.
        ("198.18.0.1", "private"),  # Benchmarking.
        ("240.0.0.1", "private"),  # Reserved.
        ("255.255.255.255", "private"),
        ("224.0.0.1", "unspecified or multicast"),
        ("239.255.255.250", "unspecified or multicast"),
        ("::1", "private"),
        ("::", "unspecified"),
        ("fe80::1", "private"),
        ("fe80::1%eth0", "private"),
        ("fc00::1", "private"),
        ("fd12:3456::1", "private"),
        ("fec0::1", "private"),  # Site-local (deprecated).
        ("ff02::1", "unspecified or multicast"),
        ("::ffff:127.0.0.1", "private"),
        ("::ffff:10.1.2.3", "private"),
        ("::ffff:169.254.169.254", "metadata"),
        ("::127.0.0.1", "private"),  # IPv4-compatible (deprecated).
        ("64:ff9b::7f00:1", "private"),  # NAT64 of 127.0.0.1.
        ("64:ff9b::a9fe:a9fe", "metadata"),  # NAT64 of 169.254.169.254.
        ("2002:7f00:1::1", "private"),  # 6to4 of 127.0.0.1.
        ("2001:0:4136:e378:8000:63bf:3fff:fdd2", "private"),  # Teredo.
        ("169.254.169.254", "metadata"),
        ("169.254.170.2", "metadata"),
        ("fd00:ec2::254", "metadata"),
        ("100.100.100.200", "metadata"),
        ("168.63.129.16", "metadata"),
        ("192.0.0.192", "metadata"),
    ],
)
def test_only_public_addresses_may_be_called(address: str, reason: str | None) -> None:
    """Private, local, reserved, multicast, and metadata addresses are refused."""
    found = refusal(ipaddress.ip_address(address))

    if reason is None:
        assert found is None
    else:
        assert found is not None
        assert reason in found


def test_allowed_private_networks_let_only_themselves_through() -> None:
    """Named networks pass; metadata, unspecified, and multicast never do."""
    allowed = (
        ipaddress.ip_network("10.20.0.0/16"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("0.0.0.0/0"),
        ipaddress.ip_network("fd00::/8"),
    )

    assert refusal(ipaddress.ip_address("10.20.3.4"), allowed[:1]) is None
    assert refusal(ipaddress.ip_address("10.21.3.4"), allowed[:1]) is not None
    assert refusal(ipaddress.ip_address("::ffff:10.20.3.4"), allowed[:1]) is None
    assert refusal(ipaddress.ip_address("fd12::1"), allowed) is None
    for never in ("169.254.169.254", "0.0.0.0", "224.0.0.1", "fd00:ec2::254"):  # noqa: S104
        assert refusal(ipaddress.ip_address(never), allowed) is not None


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("http://example.com/hook", "must use https (http only while DEBUG is on)"),
        ("ftp://example.com/hook", "must use https"),
        ("file:///etc/passwd", "must use https"),
        ("example.com/hook", "must use https"),
        ("https://user:pw@example.com/", "may not hold a user name or password"),
        ("https://user@example.com/", "may not hold a user name or password"),
        ("https://example.com/#x", "may not have a fragment"),
        ("https:///path", "has no host"),
        ("https://localhost/", "local or metadata host"),
        ("https://LOCALHOST./", "local or metadata host"),
        ("https://app.localhost/", "local or metadata host"),
        ("https://metadata.google.internal/", "local or metadata host"),
        ("https://metadata/", "local or metadata host"),
        ("https://instance-data/", "local or metadata host"),
        ("https://127.0.0.1/", "host is a private, loopback"),
        ("https://[::1]:8443/", "host is a private, loopback"),
        ("https://[::ffff:127.0.0.1]/", "host is a private, loopback"),
        (
            "https://169.254.169.254/latest/meta-data",
            "host is a cloud metadata address",
        ),
        ("https://[fe80::1%25eth0]/", "not a valid URL|host is a private"),
        ("https://[zz]/", "not a valid URL"),
        ("https://a\x00b/", "not a valid URL"),
        ("https://example.com:99999/", "port is not valid"),
        ("https://example.com:0/", "port is not valid"),
        ("https://example.com/" + "a" * 2030, "longer than 2048 characters"),
    ],
)
def test_urls_are_checked_before_any_lookup(url: str, error: str) -> None:
    """Scheme, credentials, fragments, local names, and literal addresses."""
    with pytest.raises(
        UnsafeTarget, match=error.replace("(", r"\(").replace(")", r"\)")
    ) as refused:
        checked_url(url, allow_http=False)
    assert url not in str(refused.value)
    assert refused.value.code == "unsafe_webhook_url"


def test_good_urls_pass_the_checks() -> None:
    """Public https URLs pass; http only when allowed; IDN hosts are encoded."""
    assert (
        checked_url("https://hooks.example.com:8443/a?b=1", allow_http=False).port
        == 8443
    )
    assert checked_url("http://hooks.example.com/", allow_http=True).scheme == "http"
    assert checked_url("https://bücher.example/", allow_http=False).raw_host == (
        b"xn--bcher-kva.example"
    )
    assert checked_url("https://127.0.0.1/", allow_http=False, allowed=LOCAL).host == (
        "127.0.0.1"
    )
    with pytest.raises(UnsafeTarget, match="must use https or http"):
        checked_url("ftp://a.example/", allow_http=True)
    assert checked_url("https://" + "a" * 2000 + ".example/x", allow_http=False)


@dataclass
class FakeResolver:
    """Answers lookups from a table, and counts them."""

    answers: dict[str, list[str]]
    asked: list[tuple[str, int]] = field(default_factory=list)

    async def __call__(self, host: str, port: int) -> list[Any]:
        """The addresses of ``host``."""
        self.asked.append((host, port))
        return [ipaddress.ip_address(a) for a in self.answers.get(host, [])]


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> FakeResolver:
    """Replace DNS with a table."""
    fake = FakeResolver({})
    monkeypatch.setattr(guard, "lookup", fake)
    return fake


async def test_every_address_of_a_host_must_pass(resolver: FakeResolver) -> None:
    """One private address among public ones refuses the host."""
    resolver.answers.update(
        {
            "good.example": ["93.184.215.14", "2606:4700::1111"],
            "mixed.example": ["93.184.215.14", "10.0.0.5"],
            "rebind.example": ["127.0.0.1"],
            "nothing.example": [],
        }
    )

    good = await checked_address(checked_url("https://good.example/", allow_http=False))
    assert str(good) == "93.184.215.14"
    assert resolver.asked == [("good.example", 443)]
    for host in ("mixed.example", "rebind.example"):
        with pytest.raises(UnsafeTarget, match="resolves to a private, loopback"):
            await checked_address(checked_url(f"http://{host}:8080/", allow_http=True))
    with pytest.raises(UnsafeTarget, match="has no address"):
        await checked_address(checked_url("https://nothing.example/", allow_http=False))
    assert resolver.asked[-2] == ("rebind.example", 8080)
    literal = await checked_address(
        checked_url("https://127.0.0.1/", allow_http=False, allowed=LOCAL), LOCAL
    )
    assert str(literal) == "127.0.0.1"
    assert len(resolver.asked) == 4  # Literal addresses need no lookup.


async def test_real_lookups_are_checked_too() -> None:
    """localhost-like names that resolve locally are refused by address."""
    addresses = await guard.lookup("127.0.0.1", 80)
    assert addresses == [ipaddress.ip_address("127.0.0.1")]
    with pytest.raises(socket.gaierror):
        await guard.lookup("no-such-host.invalid", 443)


# The sender ---------------------------------------------------------------------


@dataclass
class Received:
    """One request the receiver got."""

    method: str
    target: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Receiver:
    """A tiny HTTP server on 127.0.0.1 that answers from a list."""

    answers: list[tuple[int, bytes, dict[str, str]]] = field(default_factory=list)
    tls: ssl.SSLContext | None = None
    hang: bool = False
    requests: list[Received] = field(default_factory=list)
    port: int = 0
    _server: asyncio.Server | None = None
    _stopped: asyncio.Event = field(default_factory=asyncio.Event)

    async def __aenter__(self) -> "Receiver":
        """Start listening on a free port."""
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, ssl=self.tls
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Stop listening."""
        assert self._server is not None
        self._stopped.set()  # Hanging answers end now.
        self._server.close()
        await self._server.wait_closed()

    def url(self, path: str = "/hook", host: str = "127.0.0.1") -> str:
        """The URL of ``path`` here."""
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{host}:{self.port}{path}"

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Read one request and answer it."""
        try:
            method, target, _ = (await reader.readline()).decode().split(" ", 2)
            headers = {}
            while (line := await reader.readline()) not in {b"\r\n", b""}:
                name, _, value = line.decode().partition(":")
                headers[name.strip().lower()] = value.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            self.requests.append(Received(method, target, headers, body))
            if self.hang:
                await self._stopped.wait()
            status, payload, extra = (
                self.answers.pop(0) if self.answers else (200, b"ok", {})
            )
            head = f"HTTP/1.1 {status} Answer\r\nContent-Length: {len(payload)}\r\n"
            head += "".join(f"{name}: {value}\r\n" for name, value in extra.items())
            writer.write(f"{head}Connection: close\r\n\r\n".encode() + payload)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError, ValueError):
            pass
        finally:
            writer.close()


def settings_for(**options: Any) -> WebhookSettings:  # noqa: ANN401 - settings.
    """Settings that let 127.0.0.1 through, with short timeouts."""
    return WebhookSettings(
        **{
            "ALLOWED_PRIVATE_NETWORKS": ["127.0.0.0/8"],
            "TIMEOUT": 2,
            "CONNECT_TIMEOUT": 1,
            **options,
        }
    )


HEADERS = {"webhook-id": "msg_1", "webhook-timestamp": "1", "webhook-signature": "v1,x"}


async def test_a_delivery_reaches_the_receiver() -> None:
    """The body and headers arrive; 2xx is a success, with its time."""
    async with Receiver() as receiver:
        outcome = await client.post(
            receiver.url("/hook?x=1"),
            b'{"a":1}',
            HEADERS,
            settings_for(),
            allow_http=True,
        )

    assert (outcome.ok, outcome.status, outcome.error, outcome.refused) == (
        True,
        200,
        None,
        False,
    )
    assert 0 < outcome.seconds < 2
    (request,) = receiver.requests
    assert (request.method, request.target, request.body) == (
        "POST",
        "/hook?x=1",
        b'{"a":1}',
    )
    assert request.headers["host"] == f"127.0.0.1:{receiver.port}"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["user-agent"] == "domainkit-webhooks"
    assert {name: request.headers[name] for name in HEADERS} == HEADERS


async def test_the_checked_address_is_the_one_connected_to(
    resolver: FakeResolver,
) -> None:
    """The host is looked up once; the request names it but goes to the address."""
    resolver.answers["hooks.example.test"] = ["127.0.0.1"]
    async with Receiver() as receiver:
        outcome = await client.post(
            receiver.url(host="hooks.example.test"),
            b"{}",
            HEADERS,
            settings_for(),
            allow_http=True,
        )

    assert outcome.ok
    assert resolver.asked == [("hooks.example.test", receiver.port)]
    assert receiver.requests[0].headers["host"] == f"hooks.example.test:{receiver.port}"


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (
            (302, b"", {"Location": "http://169.254.169.254/"}),
            "HTTP 302: redirects are not followed.",
        ),
        ((500, b"boom", {}), "HTTP 500."),
        ((404, b"", {}), "HTTP 404."),
        ((418, b"", {}), "HTTP 418."),
    ],
)
async def test_other_answers_are_failures(
    answer: tuple[int, bytes, dict[str, str]], error: str | None
) -> None:
    """Redirects are not followed; anything but 2xx fails."""
    async with Receiver([answer]) as receiver:
        outcome = await client.post(
            receiver.url(), b"{}", HEADERS, settings_for(), allow_http=True
        )

    assert (outcome.ok, outcome.status, outcome.error) == (False, answer[0], error)
    assert len(receiver.requests) == 1


async def test_large_answers_are_cut_off() -> None:
    """At most MAX_RESPONSE_BYTES are read; the status still counts."""
    big = b"x" * 5_000_000
    async with Receiver([(200, big, {})]) as receiver:
        outcome = await client.post(
            receiver.url(),
            b"{}",
            HEADERS,
            settings_for(MAX_RESPONSE_BYTES=1024),
            allow_http=True,
        )

    assert (outcome.ok, outcome.status) == (True, 200)


async def test_slow_or_absent_receivers_fail_in_time() -> None:
    """The whole attempt has TIMEOUT seconds; refused connections fail at once."""
    async with Receiver(hang=True) as receiver:
        slow = await client.post(
            receiver.url(),
            b"{}",
            HEADERS,
            settings_for(TIMEOUT=0.3, CONNECT_TIMEOUT=0.3),
            allow_http=True,
        )
    closed = await client.post(
        receiver.url("/secret-token-path"),
        b"{}",
        HEADERS,
        settings_for(),
        allow_http=True,
    )

    assert (slow.status, slow.error) == (None, "No answer within 0.3s.")
    assert 0.25 < slow.seconds < 2
    assert closed.status is None
    assert closed.error is not None
    assert closed.error.startswith("ConnectError: ")
    assert "secret-token-path" not in closed.error


async def test_refused_targets_are_never_contacted(resolver: FakeResolver) -> None:
    """A refused URL or address fails at once, marked as refused."""
    resolver.answers["rebind.example"] = ["10.0.0.1"]
    async with Receiver() as receiver:
        outcomes = [
            await client.post(
                receiver.url(),
                b"{}",
                HEADERS,
                settings_for(ALLOWED_PRIVATE_NETWORKS=[]),
                allow_http=True,
            ),
            await client.post(
                receiver.url(), b"{}", HEADERS, settings_for(), allow_http=False
            ),
            await client.post(
                "http://rebind.example/",
                b"{}",
                HEADERS,
                settings_for(),
                allow_http=True,
            ),
        ]

    assert receiver.requests == []
    assert [(o.refused, o.status, o.ok) for o in outcomes] == [(True, None, False)] * 3
    assert outcomes[0].error == (
        "Refused: The webhook URL's host is a private, loopback, link-local, or "
        "reserved address."
    )
    assert outcomes[2].error == (
        "Refused: The webhook URL's host resolves to a private, loopback, "
        "link-local, or reserved address."
    )


async def test_errors_never_quote_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even an error whose text holds the URL (with its token) is redacted."""
    url = "http://127.0.0.1:9/hooks/T0K3N"

    messages = [f"cannot reach {url}", "x" * 1000]

    async def leak(*args: object) -> int:
        raise httpx2.ConnectError(messages.pop(0))

    monkeypatch.setattr(client, "_send", leak)
    outcome = await client.post(url, b"{}", HEADERS, settings_for(), allow_http=True)
    long = await client.post(url, b"{}", HEADERS, settings_for(), allow_http=True)

    assert outcome.error == "ConnectError: cannot reach <url>"
    assert long.error == ("ConnectError: " + "x" * 1000)[: client.MAX_ERROR_LENGTH]


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """A test CA and server certificates for hooks.example.test and another name."""
    folder = tmp_path_factory.mktemp("webhook-tls")
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Webhook test CA")])
    now = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    paths = {"ca": folder / "ca.pem"}
    paths["ca"].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for name in ("hooks.example.test", "other.example.test"):
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False
            )
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        paths[name] = folder / f"{name}.pem"
        paths[name].write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
            + key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return paths


def server_tls(chain: Path) -> ssl.SSLContext:
    """A server context presenting ``chain``."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain)
    return context


async def test_tls_checks_the_name_while_connecting_to_the_address(
    certificates: dict[str, Path], resolver: FakeResolver
) -> None:
    """SNI and the certificate check use the host name, not the pinned address."""
    resolver.answers["hooks.example.test"] = ["127.0.0.1"]
    config = settings_for(CA_BUNDLE=certificates["ca"])
    async with Receiver(tls=server_tls(certificates["hooks.example.test"])) as right:
        good = await client.post(
            right.url(host="hooks.example.test"),
            b"{}",
            HEADERS,
            config,
            allow_http=False,
        )
    async with Receiver(tls=server_tls(certificates["other.example.test"])) as wrong:
        bad = await client.post(
            wrong.url(host="hooks.example.test"),
            b"{}",
            HEADERS,
            config,
            allow_http=False,
        )
    async with Receiver(tls=server_tls(certificates["hooks.example.test"])) as unknown:
        untrusted = await client.post(
            unknown.url(host="hooks.example.test"),
            b"{}",
            HEADERS,
            settings_for(),  # The system's CAs do not know the test CA.
            allow_http=False,
        )

    assert (good.ok, good.status) == (True, 200)
    assert right.requests[0].headers["host"] == f"hooks.example.test:{right.port}"
    for failed in (bad, untrusted):
        assert failed.status is None
        assert failed.error is not None
        assert (
            "CERTIFICATE_VERIFY_FAILED" in failed.error or "certificate" in failed.error
        )
    assert wrong.requests == []


def test_tls_contexts_are_built_once_and_strict(certificates: dict[str, Path]) -> None:
    """One context per CA bundle; TLS 1.2 at least; names checked."""
    system = client._tls(None)
    bundle = client._tls(certificates["ca"])

    assert client._tls(None) is system
    assert client._tls(certificates["ca"]) is bundle
    for context in (system, bundle):
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2
        assert context.check_hostname
        assert context.verify_mode == ssl.CERT_REQUIRED


def test_settings_keep_the_connect_timeout_within_the_attempt() -> None:
    """CONNECT_TIMEOUT may not exceed TIMEOUT."""
    with pytest.raises(ValueError, match="CONNECT_TIMEOUT must not exceed TIMEOUT"):
        WebhookSettings(TIMEOUT=2, CONNECT_TIMEOUT=3)
