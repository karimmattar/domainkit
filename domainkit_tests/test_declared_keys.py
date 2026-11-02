"""Unique and indexed fields declared on the aggregate (Django's ``Meta``)."""

from collections.abc import AsyncIterator, Iterator
from typing import Annotated, NewType
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Index, MetaData, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.conf import override_settings
from domainkit.db import (
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    connections,
    map_imperatively,
    table_for,
)
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.ddd import (
    AggregateRoot,
    DuplicateValue,
    Encrypted,
    Entity,
    F,
    Indexed,
    MaxLength,
    Unique,
)
from domainkit.money import Money

pytestmark = pytest.mark.anyio

MemberId = NewType("MemberId", UUID)


class Member(AggregateRoot[MemberId]):
    """A member: a unique email, an indexed status, two keys over fields."""

    email: Annotated[str, MaxLength(80), Unique()]
    status: Annotated[str, MaxLength(10), Indexed()]
    region: Annotated[str, MaxLength(4)]
    number: int
    balance: Money

    class Meta:
        """Unique per region, and looked up by status and balance."""

        unique = (("region", "number"),)
        indexes = (("status", "balance"),)


metadata = MetaData(naming_convention=NAMING_CONVENTION)
members = table_for(Member, "framework_test_members", metadata=metadata)
map_imperatively(Member, members)


class Members(SqlAlchemyRepository[Member, MemberId]):
    """Members in SQL."""


class MembersUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the members."""

    members: Members

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.members = Members(session)


def member(email: str, number: int, region: str = "EU") -> Member:
    """A new member."""
    return Member(
        id=MemberId(uuid4()),
        email=email,
        status="active",
        region=region,
        number=number,
        balance=Money("1.00", "USD"),
    )


def test_markers_and_meta_become_named_keys() -> None:
    """One key per marker and per ``Meta`` entry, named after its fields."""
    unique = {
        constraint.name: [column.name for column in constraint.columns]
        for constraint in members.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    indexes = {
        index.name: [column.name for column in index.columns]
        for index in members.indexes
    }

    assert unique == {
        "uq_framework_test_members_email": ["email"],
        "uq_framework_test_members_region_number": ["region", "number"],
    }
    assert indexes == {
        "ix_framework_test_members_status": ["status"],
        "ix_framework_test_members_status_balance": [
            "status",
            "balance_currency",
            "balance",
        ],
    }


def test_a_long_name_is_cut_to_what_postgresql_keeps() -> None:
    """63 characters at most, the same each time, told apart by a digest."""

    class Long(AggregateRoot[int]):
        a_rather_long_field_name: Annotated[int, Indexed()]

    table = table_for(
        Long, "framework_test_a_table_with_a_long_name", metadata=MetaData()
    )
    (index,) = table.indexes

    assert isinstance(index, Index)
    assert isinstance(index.name, str)
    assert len(index.name) == 63
    assert index.name.startswith("ix_framework_test_a_table_with_a_long_name_a_rather")


def test_keys_that_cannot_be_declared_are_refused() -> None:
    """A child entity, an encrypted field, a name that is no field, a bare name."""

    class Line(Entity[int]):
        sku: str

    class WithChildren(AggregateRoot[int]):
        lines: list[Line]

        class Meta:
            indexes = (("lines",),)

    class WithSecret(AggregateRoot[int]):
        secret: Annotated[str, Encrypted(), Unique()]

    class WithTypo(AggregateRoot[int]):
        name: str

        class Meta:
            unique = (("nmae",),)

    class WithString(AggregateRoot[int]):
        name: str

        class Meta:
            unique = ("name",)

    for cls in (WithChildren, WithSecret, WithTypo):
        with pytest.raises(TypeError, match="cannot be unique or indexed"):
            table_for(cls, "framework_test_refused", metadata=MetaData())
    with pytest.raises(TypeError, match="holds tuples of field names"):
        table_for(WithString, "framework_test_refused", metadata=MetaData())


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


@pytest.fixture
async def tables(database: str) -> AsyncIterator[None]:
    """The members table, in the running loop's engine."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    yield
    await connections.close_all()


async def test_a_duplicate_names_its_fields_and_get_or_create_reads_it(
    tables: None,
) -> None:
    """The database holds the keys; the domain hears which fields clashed."""
    first = member("ann@example.com", 1)
    async with MembersUnitOfWork() as uow:
        await uow.members.add(first)
        await uow.commit()

    by_email = await refused(member("ann@example.com", 2))
    by_number = await refused(member("bob@example.com", 1))
    async with MembersUnitOfWork() as uow:
        found, created = await uow.members.get_or_create(
            F("email") == "ann@example.com", lambda: member("ann@example.com", 3)
        )

    # PostgreSQL names the constraint; SQLite names the columns.
    assert by_email.constraint == "uq_framework_test_members_email" or (
        by_email.fields == ("email",)
    )
    assert by_number.constraint == "uq_framework_test_members_region_number" or (
        set(by_number.fields) == {"region", "number"}
    )
    assert (found.id, created) == (first.id, False)


async def refused(new: Member) -> DuplicateValue:
    """The error a unit of work that adds ``new`` commits with."""
    async with MembersUnitOfWork() as uow:
        await uow.members.add(new)
        with pytest.raises(DuplicateValue) as caught:
            await uow.commit()
    return caught.value
