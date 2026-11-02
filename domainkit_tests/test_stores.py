"""Storage declared, not written: repositories from a unit of work's annotations."""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from uuid import uuid4

import pytest

from domainkit.conf import override_settings
from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork, connections
from domainkit.ddd import (
    DomainEvent,
    EventBus,
    InMemoryRepository,
    Repository,
    UnitOfWork,
    in_memory,
)
from domainkit.ddd.stores import implementation, repositories_of
from domainkit_tests.gadgets import (
    Gadget,
    GadgetId,
    InMemoryGadgets,
    make_gadget,
    metadata,
)

pytestmark = pytest.mark.anyio


class GadgetPort(Repository[Gadget, GadgetId]):
    """A port with a rule of its own, and nothing left to write."""

    def describe(self) -> str:
        """Say what it stores."""
        return f"gadgets in {type(self).__name__}"


class WithQuery(Repository[Gadget, GadgetId]):
    """A port that declares a query no adapter writes."""

    @abstractmethod
    async def cheapest(self) -> Gadget:
        """The gadget with the lowest price."""


class ShopUnitOfWork(UnitOfWork, ABC):
    """A domain's port: two repositories, one without a port of its own."""

    gadgets: GadgetPort
    catalog: Repository[Gadget, GadgetId]
    _note: GadgetPort


class SqlShop(SqlAlchemyUnitOfWork, ShopUnitOfWork):
    """Its SQL implementation, written as one line."""


class SqlGadgets(SqlAlchemyRepository[Gadget, GadgetId], GadgetPort):
    """A repository class of the application's own."""


class SqlShopWithOwn(SqlAlchemyUnitOfWork, ShopUnitOfWork):
    """Names its own class for one port."""

    gadgets: SqlGadgets


class Saved(DomainEvent):
    """Recorded to see where events go."""


def test_the_repositories_are_read_from_the_annotations() -> None:
    """Public annotations that name repositories, the most derived winning."""
    assert repositories_of(ShopUnitOfWork) == {
        "gadgets": GadgetPort,
        "catalog": Repository[Gadget, GadgetId],
    }
    assert repositories_of(SqlShopWithOwn)["gadgets"] is SqlGadgets
    assert repositories_of(UnitOfWork) == {}


def test_an_implementation_is_made_once_per_port_and_adapter() -> None:
    """Named after both; a class of the adapter is used as it is."""
    made = implementation(GadgetPort, InMemoryRepository)
    bare = implementation(Repository[Gadget, GadgetId], SqlAlchemyRepository)

    assert made is implementation(GadgetPort, InMemoryRepository)
    assert made.__name__ == "InMemoryGadgetPort"
    assert issubclass(made, GadgetPort)
    assert issubclass(made, InMemoryRepository)
    assert made().aggregate_type is Gadget
    assert bare.__name__ == "SqlAlchemyGadgetRepository"
    assert implementation(SqlGadgets, SqlAlchemyRepository) is SqlGadgets


def test_a_port_with_a_query_of_its_own_needs_its_class() -> None:
    """What the adapter cannot write is named, and so is the way out."""
    with pytest.raises(TypeError, match="WithQuery declares cheapest"):
        implementation(WithQuery, InMemoryRepository)


def test_only_ports_and_bare_repositories_can_be_annotated() -> None:
    """A parametrized port other than ``Repository[...]`` is refused."""
    with pytest.raises(TypeError, match="annotate a repository with its port class"):
        implementation(InMemoryRepository[Gadget, GadgetId], SqlAlchemyRepository)


async def _add_and_fail(uow: ShopUnitOfWork) -> None:
    """Add a gadget, then fail before committing."""
    with pytest.raises(RuntimeError, match="fails"):
        await _fail_after_adding(uow)


async def _fail_after_adding(uow: ShopUnitOfWork) -> None:
    """The work a use case does before it fails."""
    async with uow:
        await uow.gadgets.add(make_gadget("b"))
        raise RuntimeError("fails")


async def test_in_memory_counts_commits_and_rolls_back() -> None:
    """The unit of work tests need, from the port alone."""
    uow = in_memory(ShopUnitOfWork)
    gadget = make_gadget("a")

    async with uow:
        await uow.gadgets.add(gadget)
        await uow.commit()
    await _add_and_fail(uow)
    async with uow:
        found = await uow.gadgets.get(gadget.id)

    assert isinstance(uow, ShopUnitOfWork)
    assert type(uow).__name__ == "InMemoryShopUnitOfWork"
    assert (found, uow.committed) == (gadget, 1)
    assert list(uow.gadgets.items) == [gadget.id]  # type: ignore[attr-defined]
    assert uow.gadgets.describe() == "gadgets in InMemoryGadgetPort"
    assert not hasattr(uow, "_note")


async def test_in_memory_publishes_to_its_own_bus() -> None:
    """Events go to the bus given, or to one of its own; never the global one."""
    bus = EventBus()
    seen: list[DomainEvent] = []

    async def collect(event: DomainEvent) -> None:
        seen.append(event)

    bus.subscribe(Saved, collect)
    uow = in_memory(ShopUnitOfWork, events=bus)
    gadget = make_gadget("a")
    gadget.record(Saved())

    async with uow:
        await uow.gadgets.add(gadget)
        await uow.commit()

    assert uow.events is bus
    assert [type(event) for event in seen] == [Saved]
    assert in_memory(ShopUnitOfWork).events is not in_memory(ShopUnitOfWork).events


def test_in_memory_takes_repositories_of_the_test_s_own() -> None:
    """Given ones are used; a name the port lacks is refused."""
    filled = InMemoryGadgets([make_gadget("a")])

    uow = in_memory(ShopUnitOfWork, gadgets=filled)

    assert uow.gadgets is filled  # type: ignore[comparison-overlap]
    with pytest.raises(TypeError, match="has no repository orders; it annotates"):
        in_memory(ShopUnitOfWork, orders=filled)


def test_in_memory_takes_only_a_port() -> None:
    """A unit of work that stores its work somewhere is refused, as is a non-class."""
    with pytest.raises(TypeError, match="SqlShop already stores its work"):
        in_memory(SqlShop)
    with pytest.raises(TypeError, match="takes a UnitOfWork class"):
        in_memory(lambda: SqlShop())


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """The test database, its gadget tables made."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


async def test_sql_units_of_work_make_their_repositories(database: str) -> None:
    """No ``create_repositories``: each port gets a SQL repository on the session."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    gadget = make_gadget("a")

    async with SqlShop() as uow:
        await uow.gadgets.add(gadget)
        await uow.commit()
    async with SqlShopWithOwn() as uow:
        found = await uow.gadgets.get(gadget.id)
        own = uow.gadgets
        catalog = uow.catalog
        missing = await catalog.find(GadgetId(uuid4()))

    assert (found.id, found.name) == (gadget.id, "a")
    assert type(own) is SqlGadgets
    assert type(catalog).__name__ == "SqlAlchemyGadgetRepository"
    assert missing is None
