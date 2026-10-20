"""An aggregate with every kind of field, its derived tables, and repositories.

The query, paginator, and filter tests share it; the tables are on their
own metadata, so migrations never see them.
"""

from dataclasses import field
from datetime import UTC, datetime
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Annotated, Final, NewType
from uuid import UUID, uuid4

from sqlalchemy import Column, ForeignKey, MetaData, String
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import (
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    ValueObjectType,
    child,
    children,
    map_imperatively,
    table_for,
)
from domainkit.ddd import (
    AggregateRoot,
    BusinessRuleViolation,
    Digits,
    DomainEvent,
    Entity,
    EventBus,
    InMemoryRepository,
    MaxLength,
    SoftDeletable,
    UnitOfWork,
    ValueObject,
)

GadgetId = NewType("GadgetId", UUID)
PartId = NewType("PartId", UUID)
MakerId = NewType("MakerId", UUID)


class Color(StrEnum):
    """Text values."""

    RED = "red"
    GREEN = "green"
    BLUE = "blue"


class Size(IntEnum):
    """Integer values that sort differently as text (10 < 2)."""

    SMALL = 2
    LARGE = 10


class LabelTooLong(BusinessRuleViolation):
    """A label is at most 20 characters."""


class Label(ValueObject):
    """A single-field value object with a rule of its own."""

    value: Annotated[str, MaxLength(20)]

    def __post_init__(self) -> None:
        """Refuse long labels."""
        if len(self.value) > 20:
            raise LabelTooLong("A label is at most 20 characters.")


class Part(Entity[PartId]):
    """A child entity; a gadget has several."""

    sku: Annotated[str, MaxLength(10)]
    weight: int


class Maker(Entity[MakerId]):
    """A child entity; a gadget has at most one."""

    name: Annotated[str, MaxLength(20)]


class GadgetMade(DomainEvent):
    """A gadget was made."""

    gadget_id: UUID


class Gadget(SoftDeletable, AggregateRoot[GadgetId]):
    """An aggregate with a field of each kind."""

    name: Annotated[str, MaxLength(30)]
    label: Label
    price: Annotated[Decimal, Digits(10, 2)]
    made_at: datetime
    stock: int | None = None
    active: bool = True
    color: Color = Color.RED
    size: Size = Size.SMALL
    parts: list[Part] = field(default_factory=list)
    maker: Maker | None = None

    def rename(self, name: str) -> None:
        """Change the name, recording it."""
        self.name = name
        self.record(GadgetMade(gadget_id=self.id))


metadata = MetaData()
TEXT = String(30, collation="C").with_variant(String(30), "sqlite")
"""Ordered text compares by code point, as Python does (the C collation)."""
gadgets = table_for(
    Gadget,
    "framework_test_gadgets",
    Column("name", TEXT, nullable=False),
    Column(
        "label",
        ValueObjectType(
            Label, String(20, collation="C").with_variant(String(20), "sqlite")
        ),
        nullable=False,
    ),
    metadata=metadata,
)
parts = table_for(
    Part,
    "framework_test_gadget_parts",
    Column(
        "gadget_id",
        ForeignKey("framework_test_gadgets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    metadata=metadata,
)
makers = table_for(
    Maker,
    "framework_test_gadget_makers",
    Column(
        "gadget_id",
        ForeignKey("framework_test_gadgets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    metadata=metadata,
)
map_imperatively(Part, parts)
map_imperatively(Maker, makers)
map_imperatively(
    Gadget,
    gadgets,
    properties={
        "parts": children(Part, order_by=parts.c.sku),
        "maker": child(Maker),
    },
)


class GadgetRepository(SqlAlchemyRepository[Gadget, GadgetId]):
    """Gadgets in SQL; the aggregate comes from the type argument."""


class InMemoryGadgets(InMemoryRepository[Gadget, GadgetId]):
    """Gadgets in memory."""


class GadgetsUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the gadgets."""

    gadgets: GadgetRepository

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.gadgets = GadgetRepository(session)


class FakeGadgetsUnitOfWork(UnitOfWork):
    """Gadgets in memory, as a project's fake unit of work."""

    def __init__(
        self,
        gadgets: InMemoryGadgets,
        *,
        events: EventBus | None = None,
        readonly: bool | None = None,
    ) -> None:
        """Work on ``gadgets``; publish to a private bus unless given one."""
        super().__init__(events=events or EventBus(), readonly=readonly)
        self.gadgets = gadgets
        self.commits = 0

    async def _commit(self) -> None:
        """Count the commit."""
        self.commits += 1

    async def _rollback(self) -> None:
        """Nothing to undo: the repository is the state."""


MADE: Final = datetime(2026, 1, 1, tzinfo=UTC)


def make_gadget(
    name: str = "gadget",
    *,
    price: str = "1.00",
    made_at: datetime = MADE,
    stock: int | None = None,
    active: bool = True,
    color: Color = Color.RED,
    size: Size = Size.SMALL,
    parts: list[Part] | None = None,
    maker: Maker | None = None,
) -> Gadget:
    """A new gadget; its label is its name in capitals."""
    return Gadget(
        id=GadgetId(uuid4()),
        name=name,
        label=Label(name.upper()),
        price=Decimal(price),
        made_at=made_at,
        stock=stock,
        active=active,
        color=color,
        size=size,
        parts=parts or [],
        maker=maker,
    )


def make_part(sku: str, weight: int = 1) -> Part:
    """A new part."""
    return Part(id=PartId(uuid4()), sku=sku, weight=weight)


def make_maker(name: str) -> Maker:
    """A new maker."""
    return Maker(id=MakerId(uuid4()), name=name)
