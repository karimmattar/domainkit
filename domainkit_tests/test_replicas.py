"""Read replicas: read-only work reads from them; writes stay on the primary."""

import io
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from domainkit.conf import override_settings
from domainkit.conf.global_settings import Settings
from domainkit.db import connections
from domainkit.db.routing import router
from domainkit.ddd import ReadOnlyError
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import CommandError, call_command
from domainkit_tests.conftest import MakeDatabase
from domainkit_tests.gadgets import GadgetsUnitOfWork, make_gadget, metadata
from domainkit_tests.settings import SECRET_KEY

pytestmark = pytest.mark.anyio
LOCAL = "sqlite+aiosqlite:///x.sqlite3"
NETWORK = "postgresql+asyncpg://app:secret@db.internal/app"


def check(**databases: dict[str, Any]) -> Settings:
    """Validate settings with ``databases``."""
    return Settings.model_validate({"DATABASES": databases})


def test_replicas_are_other_aliases_of_one_primary() -> None:
    """A replica is another alias, not default, of one primary, with no replicas."""
    check(default={"URL": LOCAL, "REPLICAS": ["copy"]}, copy={"URL": LOCAL})
    cases: list[tuple[dict[str, dict[str, Any]], str]] = [
        ({"default": {"URL": LOCAL, "REPLICAS": ["nope"]}}, "another SQL alias"),
        ({"default": {"URL": LOCAL, "REPLICAS": ["default"]}}, "another SQL alias"),
        (
            {
                "default": {"URL": LOCAL},
                "other": {"URL": LOCAL, "REPLICAS": ["default"]},
            },
            "not 'default'",
        ),
        (
            {
                "default": {"URL": LOCAL, "REPLICAS": ["copy"]},
                "other": {"URL": LOCAL, "REPLICAS": ["copy"]},
                "copy": {"URL": LOCAL},
            },
            "cannot also be another's replica",
        ),
        (
            {
                "default": {"URL": LOCAL, "REPLICAS": ["copy"]},
                "copy": {"URL": LOCAL, "REPLICAS": ["deeper"]},
                "deeper": {"URL": LOCAL},
            },
            "replica 'copy' cannot have REPLICAS",
        ),
    ]
    for databases, message in cases:
        with pytest.raises(ValidationError, match=message):
            check(**databases)


def test_replicas_are_never_targets() -> None:
    """Not in a shard group, and no domain routed to one."""
    replicated = {"URL": LOCAL, "REPLICAS": ["copy"]}
    with pytest.raises(ValidationError, match="in a shard group"):
        Settings.model_validate(
            {
                "DATABASES": {
                    "default": replicated,
                    "copy": {"URL": LOCAL},
                    "other": {"URL": LOCAL},
                },
                "SHARDS": {"group": {"DATABASES": ["copy", "other"]}},
            }
        )
    with pytest.raises(ValidationError, match="that is not a replica"):
        Settings.model_validate(
            {
                "DATABASES": {"default": replicated, "copy": {"URL": LOCAL}},
                "DATABASE_ROUTING": {"shop": "copy"},
            }
        )


def test_deployments_hold_replicas_to_the_tls_rules() -> None:
    """A replica on the network needs verify-full like any database."""
    deployed = {
        "ENVIRONMENT": "production",
        "ALLOWED_HOSTS": ["api.example.com"],
        "SECRET_KEY": SECRET_KEY,
    }
    primary = {"URL": NETWORK, "SSLMODE": "verify-full", "REPLICAS": ["copy"]}
    with pytest.raises(ValidationError, match=r"verify-full.*\['copy'\]"):
        Settings.model_validate(
            {**deployed, "DATABASES": {"default": primary, "copy": {"URL": NETWORK}}}
        )
    secured = {"URL": NETWORK, "SSLMODE": "verify-full"}
    Settings.model_validate(
        {**deployed, "DATABASES": {"default": primary, "copy": secured}}
    )


@pytest.fixture
def replicated(make_database: MakeDatabase) -> Iterator[tuple[str, str, str]]:
    """``default`` with two replicas, each its own empty database."""
    urls = make_database("primary"), make_database("one"), make_database("two")
    with override_settings(
        DATABASES={
            "default": {"URL": urls[0], "REPLICAS": ["one", "two"]},
            "one": {"URL": urls[1]},
            "two": {"URL": urls[2]},
        }
    ):
        yield urls


def test_read_only_work_takes_turns_on_the_replicas(
    replicated: tuple[str, str, str],
) -> None:
    """Each read-only unit of work picks the next replica; others the primary."""
    aliases = [GadgetsUnitOfWork(readonly=True).alias for _ in range(4)]

    assert sorted(aliases) == ["one", "one", "two", "two"]
    assert aliases[0] != aliases[1]
    assert GadgetsUnitOfWork().alias == "default"
    assert router.read_alias("one") == "one"
    assert router.primary_of("two") == "default"
    assert router.primary_of("default") is None


def test_nothing_writes_to_a_replica(replicated: tuple[str, str, str]) -> None:
    """A unit of work, or migrations, refuse a replica named as a target."""
    with pytest.raises(
        ImproperlyConfigured, match="'one' is a read replica of 'default'"
    ):
        GadgetsUnitOfWork(database="one")
    with pytest.raises(ImproperlyConfigured, match="readonly=True"):
        router.aliases_of("two")
    with pytest.raises((ImproperlyConfigured, CommandError), match="read replica"):
        call_command("migrate", "--database", "one", stdout=io.StringIO())


def test_without_replicas_reads_stay_on_the_primary() -> None:
    """No REPLICAS: read-only work uses the alias itself."""
    with override_settings(DATABASES={"default": {"URL": LOCAL}}):
        assert GadgetsUnitOfWork(readonly=True).alias == "default"


@pytest.fixture
async def tables(replicated: tuple[str, str, str]) -> AsyncIterator[None]:
    """The gadget tables in the primary and both replicas."""
    for alias in ("default", "one", "two"):
        async with connections.engine(alias).begin() as connection:
            await connection.run_sync(metadata.create_all)
    yield
    await connections.close_all()


async def test_read_only_work_reads_what_the_replica_holds(tables: None) -> None:
    """Writes go to the primary; reads go to a replica (here, apart from it)."""
    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.add(make_gadget("written"))
        await uow.commit()
    for alias, name in (("one", "copied"), ("two", "copied too")):
        async with AsyncSession(connections.engine(alias)) as session:
            session.add(make_gadget(name))
            await session.commit()
    seen = set()
    for _ in range(2):
        async with GadgetsUnitOfWork(readonly=True) as uow:
            (found,) = await uow.gadgets.filter()
            seen.add(found.name)
            with pytest.raises(ReadOnlyError):
                await uow.commit()
    assert seen == {"copied", "copied too"}
    async with GadgetsUnitOfWork() as uow:
        assert [each.name for each in await uow.gadgets.filter()] == ["written"]


async def test_read_only_sessions_open_on_any_database(
    replicated: tuple[str, str, str],
) -> None:
    """The READ ONLY option is PostgreSQL's; elsewhere it is ignored."""
    session = connections.session("one", readonly=True)
    plain = connections.session("one")

    assert isinstance(session.bind, AsyncEngine)
    assert session.bind.get_execution_options()["postgresql_readonly"] is True
    assert isinstance(plain.bind, AsyncEngine)
    assert "postgresql_readonly" not in plain.bind.get_execution_options()
    async with session:
        assert await session.scalar(select(literal(1))) == 1
    await connections.close_all()
