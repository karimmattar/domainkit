"""Task priorities under a broker (user report W9, ADR 0157).

RabbitMQ's quorum queues hand out their waiting messages by the priority
each carries, so the relay gives each message its task's. Brokers without
priorities are never given one, adapters written before 1.x had them
included, and ``check`` says where a task's priority stops counting
(``messaging.W004``). ``test_rabbitmq.py`` shows the order on the fake and
on real servers.
"""

from collections.abc import Iterable

import pytest

from domainkit.broker import Broker, Handler, MemoryBroker, Subscription
from domainkit.broker.base import publish_with_priority
from domainkit.broker.handler import broker_class
from domainkit.broker.rabbitmq import RabbitMQBroker, amqp_priority
from domainkit.broker.redis_streams import RedisStreamsBroker
from domainkit.conf import BrokerConfig, override_settings, settings
from domainkit.contrib.messaging import apps as apps_module
from domainkit.contrib.messaging import registry as tasks
from domainkit.contrib.messaging.apps import MessagingConfig
from domainkit.contrib.messaging.breaker import Breaker, Watched
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.test_messaging import messaging  # noqa: F401

pytestmark = pytest.mark.anyio

REDIS = {"work": {"URL": "redis://cache.example/0", "PREFIX": "t"}}
NOT_A_BROKER = {"URL": "x://h", "PREFIX": "t", "BACKEND": "domainkit.conf.BrokerConfig"}


class Published(Broker):
    """Keeps what it is given: an adapter written before brokers had priorities."""

    def __init__(self, config: BrokerConfig | None = None) -> None:
        """Keep nothing yet; ``config`` is ``BACKEND``'s."""
        self.published: list[tuple[str, dict[str, object]]] = []

    async def declare(self, queues: Iterable[str]) -> None:
        """Nothing to declare."""

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Keep the message, with no priority to take."""
        self.published.append((queue, {"message_id": message_id}))

    async def consume(
        self, queues: Iterable[str], prefetch: int, handler: Handler
    ) -> Subscription:
        """Not used here."""
        raise NotImplementedError


class Prioritized(Published):
    """An adapter whose broker hands out messages by priority."""

    priorities = True

    async def publish(
        self, queue: str, body: bytes, *, message_id: str, priority: int = 0
    ) -> None:
        """Keep the message and its priority."""
        self.published.append((queue, {"message_id": message_id, "priority": priority}))


async def nothing() -> None:
    """A task's function, never run here."""


def test_amqp_priorities_keep_the_order_of_task_priorities() -> None:
    """-100 to 100 onto a quorum queue's 0 to 31: in order, 0 as no priority.

    0 is 4, what RabbitMQ 4.3 gives a message without one; a positive
    priority is above 4, high for RabbitMQ 4.0 to 4.2, and the rest is not.
    """
    levels = [amqp_priority(priority) for priority in range(-100, 101)]

    assert levels == sorted(levels)
    assert (levels[0], amqp_priority(0), levels[-1]) == (0, 4, 31)
    assert all(amqp_priority(p) > 4 for p in range(1, 101))
    assert all(amqp_priority(p) < 4 for p in range(-100, 0))
    assert len(set(levels)) == 32  # Every level is used.


async def test_only_brokers_with_priorities_are_given_one() -> None:
    """An adapter whose ``publish`` takes no priority keeps working."""
    old, new = Published(), Prioritized()

    for broker in (old, new):
        await publish_with_priority(broker, "q", b"{}", message_id="m", priority=50)

    assert old.published == [("q", {"message_id": "m"})]
    assert new.published == [("q", {"message_id": "m", "priority": 50})]
    assert (Broker.priorities, MemoryBroker.priorities) == (False, False)
    assert RabbitMQBroker.priorities is True


async def test_the_breaker_passes_priorities_to_the_broker_it_watches() -> None:
    """Commits publish through the breaker: it takes priorities as its broker does."""
    breaker = Breaker("work", 5, 30, observed=False)
    old, new = Published(), Prioritized()
    watched_old, watched_new = Watched(old, breaker), Watched(new, breaker)

    await watched_old.publish("q", b"{}", message_id="m", priority=7)
    await watched_new.publish("q", b"{}", message_id="m", priority=7)

    assert (watched_old.priorities, watched_new.priorities) == (False, True)
    assert old.published == [("q", {"message_id": "m"})]
    assert new.published == [("q", {"message_id": "m", "priority": 7})]


def test_broker_class_names_the_adapter_without_making_it() -> None:
    """``check`` reads what a broker can do from its class."""

    def kind(url: str, **options: str) -> type[Broker]:
        config = {"URL": url, "PREFIX": "t", **options}
        return broker_class(BrokerConfig.model_validate(config))

    path = f"{__name__}.Prioritized"
    assert kind("memory://") is MemoryBroker
    assert kind("amqp://u:p@h/") is RabbitMQBroker
    assert kind("redis://h/0") is RedisStreamsBroker
    assert kind("sqlitequeue:///q.db", BACKEND=path) is Prioritized
    with pytest.raises(
        ImproperlyConfigured, match=r"is not a domainkit\.broker\.Broker"
    ):
        kind("x://h", BACKEND="domainkit.conf.BrokerConfig")
    with pytest.raises(ImproperlyConfigured, match="BROKERS BACKEND"):
        kind("x://h", BACKEND="domainkit_tests.nowhere.Broker")


@pytest.mark.usefixtures("messaging")
def test_priorities_a_broker_drops_are_warned_about_where_deployed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``messaging.W004``: a task's priority under a broker that has none."""

    @tasks.task(name="tests.priorities.urgent", priority=50)
    async def urgent() -> None: ...

    @tasks.task(name="tests.priorities.plain")
    async def plain() -> None: ...

    config_ = MessagingConfig.__new__(MessagingConfig)
    try:
        with override_settings(BROKERS=REDIS, MESSAGING={"BROKER": "work"}):
            assert config_.unkept_priorities() == []  # Not deployed.
            monkeypatch.setattr(
                apps_module, "DEPLOYED_ENVIRONMENTS", frozenset({settings.ENVIRONMENT})
            )
            (found,) = config_.unkept_priorities()
        with override_settings(
            BROKERS={"work": {"URL": "amqps://u:p@mq.example/", "PREFIX": "t"}},
            MESSAGING={"BROKER": "work"},
        ):
            assert config_.unkept_priorities() == []  # RabbitMQ keeps them.
        with override_settings(BROKERS=REDIS, MESSAGING={"BROKER": None}):
            assert config_.unkept_priorities() == []  # The outbox orders them.
    finally:
        for name in ("tests.priorities.urgent", "tests.priorities.plain"):
            tasks.unregister_task(name)

    assert found.id == "messaging.W004"
    assert "'tests.priorities.urgent'" in found.message
    assert "'tests.priorities.plain'" not in found.message
    assert "MESSAGING.BROKER ('work', a RedisStreamsBroker)" in found.message
    assert found.hint is not None
    assert found.hint.startswith("Give urgent work a queue of its own")


@pytest.mark.usefixtures("messaging")
def test_no_warning_without_priorities_or_with_a_broker_that_cannot_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to say when no task names a priority, or the broker is refused.

    A ``BACKEND`` that is not a broker stops the worker when it starts;
    the warning says nothing of it.
    """
    config_ = MessagingConfig.__new__(MessagingConfig)
    monkeypatch.setattr(
        apps_module, "DEPLOYED_ENVIRONMENTS", frozenset({settings.ENVIRONMENT})
    )
    plain = tasks.TaskSpec("tests.priorities.none", nothing)
    with override_settings(BROKERS=REDIS, MESSAGING={"BROKER": "work"}):
        monkeypatch.setattr(tasks, "_tasks", {plain.key: plain})
        assert config_.unkept_priorities() == []
        urgent = tasks.TaskSpec("tests.priorities.some", nothing, priority=1)
        monkeypatch.setattr(tasks, "_tasks", {urgent.key: urgent})
        assert len(config_.unkept_priorities()) == 1
    with override_settings(
        BROKERS={"work": NOT_A_BROKER},
        MESSAGING={"BROKER": "work"},
    ):
        assert config_.unkept_priorities() == []
