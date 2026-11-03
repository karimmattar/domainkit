"""``@handles``: handlers marked in controllers.py, subscribed when ready."""

from collections.abc import Iterator
from types import ModuleType

import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.ddd import DomainEvent, EventBus, event_bus, handles
from domainkit.ddd.events import subscribe_marked
from domainkit_tests.conftest import ALPHA, MakePackage


class Shipped(DomainEvent):
    """Something left."""


class Lost(DomainEvent):
    """Something went missing."""


@handles(Shipped, durable=True)
@handles(Lost, timeout=2.0)
async def tell(event: DomainEvent) -> None:
    """Handles two events."""


def test_marked_handlers_of_a_module_are_subscribed() -> None:
    """Each mark is one subscription, as subscribe() makes it; imports are left."""
    module = ModuleType(__name__)
    module.tell = tell  # type: ignore[attr-defined]
    module.imported = subscribe_marked  # type: ignore[attr-defined]
    bus = EventBus()

    assert subscribe_marked(module, bus) == 2
    assert bus.handlers_for(Shipped, durable=True) == [tell]
    assert bus.handlers_for(Lost) == [tell]
    assert bus.handlers_for(Shipped) == []
    other = ModuleType("elsewhere")
    other.tell = tell  # type: ignore[attr-defined]
    assert subscribe_marked(other, EventBus()) == 0


@pytest.fixture
def ready_domain(make_package: MakePackage) -> Iterator[None]:
    """A four-file domain whose controllers.py marks a handler, installed."""
    make_package(
        "dispatch",
        {
            "models.py": "",
            "controllers.py": (
                "from domainkit.ddd import handles\n"
                "from domainkit_tests.test_handles import Shipped\n\n\n"
                "@handles(Shipped, durable=True)\n"
                "async def on_shipped(event: Shipped) -> None:\n"
                "    pass\n"
            ),
        },
    )
    registry.clear()
    with (
        event_bus.subscriptions_kept(),
        override_settings(INSTALLED_DOMAINS=[ALPHA, "dispatch"]),
    ):
        setup()
        yield
    registry.clear()


@pytest.mark.usefixtures("ready_domain")
def test_a_four_file_domain_subscribes_its_marked_handlers_when_ready() -> None:
    """No apps.py: the default DomainConfig reads controllers.py."""
    names = [
        handler.__name__ for handler in event_bus.handlers_for(Shipped, durable=True)
    ]

    assert names == ["on_shipped"]
