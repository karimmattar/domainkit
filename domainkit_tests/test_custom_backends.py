"""``BACKEND``: a cache store, or a broker, of the project's own."""

from collections.abc import Iterable

import pytest
from pydantic import ValidationError

from domainkit.broker import Broker, Handler, Subscription
from domainkit.broker.handler import BrokerHandler, connect_broker
from domainkit.cache import MemoryStore, Store
from domainkit.cache.handler import CacheHandler
from domainkit.conf import BrokerConfig, CacheConfig, override_settings
from domainkit.exceptions import ImproperlyConfigured

pytestmark = pytest.mark.anyio


class TaggedStore(MemoryStore):
    """A memory store that keeps the config it was made with."""

    def __init__(self, config: CacheConfig) -> None:
        """Keep ``config``."""
        super().__init__(config.MAX_ENTRIES)
        self.config = config


class NoSubscription(Subscription):
    """Nothing to stop."""

    async def cancel(self) -> None:
        """Nothing."""


class ListBroker(Broker):
    """A broker that keeps what it is handed, made from its config."""

    def __init__(self, config: BrokerConfig) -> None:
        """Keep ``config``; no messages yet."""
        self.config = config
        self.published: list[tuple[str, bytes]] = []

    async def declare(self, queues: Iterable[str]) -> None:
        """Nothing to make."""

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Keep it."""
        self.published.append((queue, body))

    async def consume(
        self, queues: Iterable[str], prefetch: int, handler: Handler
    ) -> Subscription:
        """Hand nothing out."""
        return NoSubscription()


CUSTOM_CACHE = {"URL": "files:///var/cache", "BACKEND": f"{__name__}.TaggedStore"}
CUSTOM_BROKER = {
    "URL": "lists://local",
    "PREFIX": "app",
    "BACKEND": f"{__name__}.ListBroker",
}


async def test_a_cache_store_of_the_projects_own() -> None:
    """Its URL's scheme is its own; the handler makes it from the config."""
    with override_settings(CACHES={"default": CUSTOM_CACHE}):
        handler = CacheHandler()
        cache = handler["default"]
        await cache.set("k", {"v": 1})

        assert await cache.get("k") == {"v": 1}
        assert isinstance(cache.store, TaggedStore)
        assert cache.store.config.URL.get_secret_value() == "files:///var/cache"
        await handler.close_all()


async def test_a_broker_of_the_projects_own() -> None:
    """Its URL is its own, a PREFIX is still asked for; made from the config."""
    with override_settings(BROKERS={"default": CUSTOM_BROKER}):
        broker = BrokerHandler()["default"]
        await broker.publish("jobs", b"{}", message_id="1")

        assert isinstance(broker, ListBroker)
        assert broker.published == [("jobs", b"{}")]
        assert broker.config.PREFIX == "app"
    with pytest.raises(ValidationError, match="PREFIX"):
        BrokerConfig.model_validate({**CUSTOM_BROKER, "PREFIX": None})
    # A known scheme keeps its own checks, BACKEND or not.
    with pytest.raises(ValidationError, match="needs a host"):
        BrokerConfig.model_validate({**CUSTOM_BROKER, "URL": "amqp://"})


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("domainkit_tests.nowhere.Store", "BACKEND: "),
        ("collections.OrderedDict", "is not a domainkit"),
    ],
)
def test_backends_must_name_their_kind(path: str, message: str) -> None:
    """A path that does not import, or names another kind, is refused."""
    from domainkit.cache.handler import custom_store

    with pytest.raises(ImproperlyConfigured, match=message):
        custom_store(
            path, CacheConfig.model_validate({**CUSTOM_CACHE, "BACKEND": path})
        )
    with pytest.raises(ImproperlyConfigured, match=message):
        connect_broker(BrokerConfig.model_validate({**CUSTOM_BROKER, "BACKEND": path}))


def test_unknown_schemes_need_a_backend() -> None:
    """Without BACKEND, a scheme domainkit does not know is refused."""
    for config, data in ((CacheConfig, CUSTOM_CACHE), (BrokerConfig, CUSTOM_BROKER)):
        with pytest.raises(ValidationError, match="starts with"):
            config.model_validate({**data, "BACKEND": None})
        with pytest.raises(ValidationError, match="BACKEND"):
            config.model_validate({**data, "BACKEND": "not a path"})


def test_stores_and_brokers_are_the_ports_to_fill() -> None:
    """What a store and a broker of one's own subclass."""
    assert issubclass(TaggedStore, Store)
    assert {"get_many", "set_many"} <= set(Store.__abstractmethods__)
    assert {"declare", "publish", "consume"} == set(Broker.__abstractmethods__)
