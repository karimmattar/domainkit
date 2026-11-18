"""Backends made per call share one HTTP client: its circuits and pool are kept.

``get_sms_backend()``, ``search_backend()``, ``identity.services.provider()``
and every captcha check made a client of their own each call, so a
provider that was down was called and waited for every time, its circuit
never opened, and each call opened a connection (audit A28-ARCH-02). They
now take a client shared by every backend of the same options, as mail
and push did; ``close_all`` closes it when the process stops, and the test
plugin closes its circuits before each test.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field

import httpx2
import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.identity import ProviderError, providers
from domainkit.contrib.identity.providers import make_provider
from domainkit.contrib.identity.settings import ProviderSettings
from domainkit.contrib.notifications import NotificationFailed, get_sms_backend
from domainkit.contrib.notifications.messages import SmsMessage
from domainkit.contrib.notifications.sms import TwilioSmsBackend
from domainkit.outbound.client import (
    HttpClient,
    close_all,
    every_client,
    reset_circuits,
    stub,
)
from domainkit.search import SearchError, search_backend
from domainkit.testing import RESETS
from domainkit_tests.conftest import Api
from domainkit_tests.test_identity_providers import OIDC

TWILIO = {"ACCOUNT_SID": "AC1", "AUTH_TOKEN": "secret", "FROM": "+15005550006"}
SMS = SmsMessage(to="+33612345678", body="Your code is 123456")
FAILURES = 5
"""``HTTP_CLIENT.BREAKER_FAILURES``'s default: a circuit opens after as many."""


class Down:
    """A provider that answers 503 to every call, and counts them."""

    def __init__(self) -> None:
        """No call yet."""
        self.calls = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Count the call; answer 503."""
        self.calls += 1
        return httpx2.Response(503)


@pytest.fixture
def no_retries() -> Iterator[None]:
    """One attempt per call, so each call is one failure against the host."""
    with override_settings(HTTP_CLIENT={"RETRIES": 0}):
        yield


@pytest.fixture
def notifications(database_url: str) -> Iterator[None]:
    """Notifications installed, sending text messages through Twilio."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.messaging",
            "domainkit.contrib.notifications",
            "domainkit_tests.sample_domains.alpha",
        ],
        DATABASES={"default": {"URL": database_url}},
        NOTIFICATIONS={
            "SMS_BACKEND": "domainkit.contrib.notifications.sms.TwilioSmsBackend",
            "SMS_OPTIONS": TWILIO,
        },
    ):
        setup()
        yield
    registry.clear()


@pytest.mark.anyio
@pytest.mark.usefixtures("notifications")
async def test_text_message_backends_share_a_client_whose_circuit_opens() -> None:
    """A backend per send, as the worker makes them: the provider is spared.

    Before, each of the twelve sends reached the provider.
    """
    first, second = get_sms_backend(), get_sms_backend()
    assert first is not second
    assert isinstance(first, TwilioSmsBackend)
    assert isinstance(second, TwilioSmsBackend)
    assert first.client is second.client
    provider = Down()
    failures: list[str] = []

    with stub(provider):
        for _ in range(12):
            with pytest.raises(NotificationFailed) as failed:
                await get_sms_backend().send_messages([SMS])
            failures.append(str(failed.value))

    assert provider.calls == FAILURES
    assert failures[-1] == "Twilio could not be reached (CircuitOpen)."


@dataclass
class KeepAlive:
    """An HTTP/1.1 server on 127.0.0.1 that keeps its connections open.

    It answers every request with ``answer`` (JSON), and counts the
    connections it accepted and those its clients closed.
    """

    answer: bytes = b'{"hits": [], "estimatedTotalHits": 0}'
    accepted: int = 0
    closed: int = 0
    port: int = 0
    _server: asyncio.Server | None = None
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    async def __aenter__(self) -> "KeepAlive":
        """Start listening on a free port."""
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Stop listening, and end the connections still open."""
        assert self._server is not None
        self._server.close()
        self._server.close_clients()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        """Where it listens."""
        return f"http://127.0.0.1:{self.port}"

    async def wait_closed(self, count: int) -> None:
        """Wait until clients closed ``count`` connections."""
        async with asyncio.timeout(10):
            while self.closed < count:
                self._changed.clear()
                await self._changed.wait()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Answer each request on the connection until the client closes it."""
        self.accepted += 1
        try:
            while line := await reader.readline():
                if not line.strip():
                    continue
                length = 0
                while (header := await reader.readline()) not in {b"\r\n", b""}:
                    name, _, value = header.decode().partition(":")
                    if name.strip().lower() == "content-length":
                        length = int(value)
                await reader.readexactly(length)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(self.answer)}\r\n\r\n".encode()
                    + self.answer
                )
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.closed += 1
            self._changed.set()
            writer.close()


@pytest.fixture
async def engine(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[KeepAlive]:
    """A search engine on this machine, reached without a proxy."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    async with KeepAlive() as server:
        yield server
    await close_all()


@pytest.mark.anyio
async def test_search_backends_reuse_one_connection(engine: KeepAlive) -> None:
    """Two backends, two searches: one connection, closed with the clients.

    Before, each backend opened a connection of its own, and none was
    closed but by the garbage collector.
    """
    meilisearch = "domainkit.search.meilisearch.MeilisearchBackend"
    with override_settings(
        SEARCH={"BACKEND": meilisearch, "OPTIONS": {"url": engine.url}}
    ):
        first, second = search_backend(), search_backend()
        assert first is not second
        assert first.client is second.client  # type: ignore[attr-defined]

        for backend in (first, second):
            found = await backend.search("products", "red shoes")
            assert found.ids == []

    assert engine.accepted == 1
    await close_all()  # As the application's shutdown does.
    await engine.wait_closed(1)


@pytest.mark.anyio
@pytest.mark.usefixtures("no_retries")
async def test_a_search_engine_that_is_down_opens_its_circuit() -> None:
    """``search_backend()`` per event, as the docs show: the engine is spared."""
    opensearch = "domainkit.search.opensearch.OpenSearchBackend"
    engine = Down()
    errors: list[str] = []
    with (
        override_settings(
            SEARCH={"BACKEND": opensearch, "OPTIONS": {"url": "https://os.example"}}
        ),
        stub(engine),
    ):
        for number in range(8):
            with pytest.raises(SearchError) as failed:
                await search_backend().index("products", [{"id": str(number)}])
            errors.append(str(failed.value))

    assert engine.calls == FAILURES
    assert errors[-1] == "OpenSearch could not be reached (CircuitOpen)."


@pytest.fixture
def fresh_providers() -> Iterator[None]:
    """No discovery document or key kept between tests."""
    providers.forget()
    yield
    providers.forget()


@pytest.mark.anyio
@pytest.mark.usefixtures("no_retries", "fresh_providers")
async def test_identity_providers_share_a_client_whose_circuit_opens() -> None:
    """A provider made per login: one that is down is called no more."""
    config = ProviderSettings(**OIDC)
    first, second = make_provider("idp", config), make_provider("idp", config)
    assert first.client is second.client
    issuer = Down()

    with stub(issuer):
        for _ in range(8):
            with pytest.raises(ProviderError):
                await make_provider("idp", config).authorization_url("s", "n", "v")

    assert issuer.calls == FAILURES


@pytest.mark.usefixtures("no_retries")
def test_a_captcha_provider_that_is_down_opens_its_circuit(identity: Api) -> None:
    """Every check is refused 503, as before; the provider is spared after five."""
    client = identity(CAPTCHA={"PROVIDER": "turnstile", "SECRET": "site-secret"})
    provider = Down()
    answers: list[tuple[int, str]] = []

    with stub(provider, everywhere=True):
        for _ in range(8):
            answer = client.post(
                "/auth/login",
                json={"username": "x", "password": "y"},
                headers={"X-Captcha-Token": "t"},
            )
            answers.append((answer.status_code, answer.json()["code"]))

    assert answers == [(503, "captcha_unavailable")] * 8
    assert provider.calls == FAILURES


def test_shared_clients_are_named_in_their_metrics() -> None:
    """Their calls and circuits are counted under their name, not ``other``."""
    from domainkit.contrib.auth import captcha
    from domainkit.contrib.mail.providers import PostmarkBackend

    sms = TwilioSmsBackend(TWILIO)
    mail = PostmarkBackend({"SERVER_TOKEN": "token"})
    oidc = make_provider("idp", ProviderSettings(**OIDC))

    assert sms.client.name == "notifications"
    assert mail.client.name == "mail"
    assert oidc.client.name == "identity"
    assert captcha._verifier().name == "captcha"


@pytest.mark.anyio
@pytest.mark.usefixtures("no_retries")
async def test_reset_circuits_closes_every_clients_circuits() -> None:
    """What the test plugin calls before each test: no circuit stays open."""
    api = HttpClient(name="shared-clients-test")
    with stub(Down()):
        for _ in range(FAILURES):
            await api.get("https://down.example/")
    assert api.breaker is not None
    assert api.breaker.open_circuits() == 1
    assert api in every_client()

    reset_circuits()

    assert api.breaker.open_circuits() == 0
    assert ("domainkit.outbound.client", "reset_circuits") in RESETS
    unbroken = HttpClient(breaker=False)
    reset_circuits()  # A client without circuits is passed over.
    assert unbroken.breaker is None
