"""Typed hooks: a domain registers what it takes part in, and only it is called."""

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit import setup
from domainkit.apps import registry
from domainkit.apps.hooks import Hook, HookPoint, hooks_kept
from domainkit.conf import override_settings
from domainkit.db import SqlAlchemyUnitOfWork, connections
from domainkit.db import uow as uow_module
from domainkit.db.hooks import (
    on_committed,
    on_migrated,
    on_migrated_committed,
    on_stage,
    on_stage_transaction,
    on_transaction_committed,
)
from domainkit.db.migrations import run_migrated_hooks
from domainkit.exceptions import ImproperlyConfigured
from domainkit.tenancy import tenant_schema
from domainkit_tests.conftest import ALPHA, MakePackage, on_server

TYPED = """
from domainkit.apps import DomainConfig
from domainkit.db.hooks import on_committed, on_migrated, on_stage
from hooklog import CALLS


class TypedConfig(DomainConfig):
    def ready(self):
        on_stage(self.first)
        on_stage(self.second)
        on_committed(self.done)
        on_migrated(self.filled)

    async def first(self, uow, events, tasks):
        CALLS.append((self.label, "stage", uow.alias))

    async def second(self, uow, events, tasks):
        CALLS.append((self.label, "stage again", uow.alias))

    async def done(self, uow):
        CALLS.append((self.label, "committed", uow.alias))

    async def filled(self, connection, database):
        CALLS.append((self.label, "migrated", database))
"""
EARLY = """
from domainkit.apps import DomainConfig
from domainkit.db.hooks import on_committed, on_stage
from hooklog import CALLS


class EarlyConfig(DomainConfig):
    def ready(self):
        on_committed(self.registered)
        on_stage(self.staged)
        on_committed(self.followed)

    async def registered(self, uow):
        CALLS.append((self.label, "registered"))

    async def staged(self, uow, events, tasks):
        CALLS.append((self.label, "stage", uow.alias))

    async def followed(self, uow):
        CALLS.append((self.label, "committed", uow.alias))
"""
FAILING = """
from domainkit.apps import DomainConfig
from domainkit.db.hooks import on_committed


class FailingConfig(DomainConfig):
    def ready(self):
        on_committed(self.fail)

    async def fail(self, uow):
        raise RuntimeError("the follow-up failed")
"""

FOLLOWED = """
from domainkit.apps import DomainConfig
from domainkit.db.hooks import on_migrated, on_migrated_committed
from hooklog import CALLS


async def path(connection):
    if connection.dialect.name != "postgresql":
        return None
    found = await connection.exec_driver_sql("SELECT current_setting('search_path')")
    return found.scalar()


class FollowedConfig(DomainConfig):
    def ready(self):
        on_migrated_committed(self.built)
        on_migrated(self.filled)

    async def filled(self, connection, database):
        await connection.exec_driver_sql("CREATE TABLE followed_rows (x INTEGER)")
        CALLS.append(("followed", "migrated", database, await path(connection)))

    async def built(self, connection, database):
        await connection.exec_driver_sql("SELECT count(*) FROM followed_rows")
        mode = connection.sync_connection.get_execution_options()["isolation_level"]
        CALLS.append(("followed", "built", database, mode, await path(connection)))
        return "followed_rows read"
"""
BREAKING = """
from domainkit.apps import DomainConfig
from domainkit.db.hooks import on_migrated


class BreakingConfig(DomainConfig):
    def ready(self):
        on_migrated(self.fail)

    async def fail(self, connection, database):
        raise RuntimeError("the hook failed")
"""
UNFINISHED = """
from domainkit.apps import DomainConfig
from domainkit.db.hooks import on_migrated, on_migrated_committed


class UnfinishedConfig(DomainConfig):
    def ready(self):
        on_migrated(self.filled)
        on_migrated_committed(self.fail)

    async def filled(self, connection, database):
        await connection.exec_driver_sql("CREATE TABLE kept_rows (x INTEGER)")

    async def fail(self, connection, database):
        raise RuntimeError("the follow-up failed")
"""


FULL_CONTRIB = [
    f"domainkit.contrib.{app}"
    for app in (
        "contenttypes",
        "auth",
        "audit",
        "messaging",
        "idempotency",
        "evidence",
        "flags",
        "mail",
        "notifications",
        "objectpermissions",
        "privacy",
        "webhooks",
    )
]
"""Every contrib app, after those it requires."""


class EmptyUnitOfWork(SqlAlchemyUnitOfWork):
    """A unit of work with no repositories, on the default database."""

    database = "default"

    def create_repositories(self, session: AsyncSession) -> None:
        """Nothing to bind."""


@pytest.fixture
def calls(make_package: MakePackage) -> list[tuple[str, ...]]:
    """What the hooks of the throwaway domains did, in order."""
    make_package("hooklog", {"__init__.py": "CALLS = []\n"})
    make_package("typed", {"apps.py": TYPED})
    make_package("early", {"apps.py": EARLY})
    make_package("quiet", {})
    make_package("failing", {"apps.py": FAILING})
    make_package("followed", {"apps.py": FOLLOWED})
    make_package("breaking", {"apps.py": BREAKING})
    make_package("unfinished", {"apps.py": UNFINISHED})
    import hooklog  # type: ignore[import-not-found]

    return hooklog.CALLS  # type: ignore[no-any-return]


@pytest.fixture
def installed(calls: list[tuple[str, ...]], tmp_path: Path) -> Iterator[None]:
    """The throwaway domains installed, on a SQLite file."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'hooks.sqlite3'}"
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "quiet", "early", "typed"],
        DATABASES={"default": {"URL": url}},
    ):
        setup()
        yield
    asyncio.run(connections.close_all())


def commit() -> None:
    """Commit an empty unit of work."""

    async def run() -> None:
        async with EmptyUnitOfWork() as uow:
            await uow.commit()
        await connections.close_all()

    asyncio.run(run())


@pytest.mark.usefixtures("installed")
@pytest.mark.usefixtures("database_access")
def test_only_domains_that_registered_are_called(calls: list[tuple[str, ...]]) -> None:
    """In domain order, each domain's in the order it registered (DSGN-03)."""
    assert [hook.domain for hook in on_stage] == ["early", "typed", "typed"]
    assert [hook.domain for hook in on_committed] == ["early", "early", "typed"]
    assert list(on_stage_transaction) == []
    assert list(on_transaction_committed) == []

    commit()

    assert calls == [
        ("early", "stage", "default"),
        ("typed", "stage", "default"),
        ("typed", "stage again", "default"),
        ("early", "registered"),
        ("early", "committed", "default"),
        ("typed", "committed", "default"),
    ]


@pytest.mark.usefixtures("installed")
@pytest.mark.usefixtures("database_access")
def test_migrated_hooks_run_on_the_migrated_database(
    calls: list[tuple[str, ...]],
) -> None:
    """``run_migrated_hooks`` calls ``on_migrated`` hooks with the alias."""
    run_migrated_hooks("default")

    assert calls == [("typed", "migrated", "default")]


@pytest.mark.usefixtures("database_access")
def test_migrated_follow_ups_run_once_the_hooks_committed(
    calls: list[tuple[str, ...]], tmp_path: Path
) -> None:
    """``on_migrated_committed`` hooks run after that transaction, outside one.

    Each of their statements commits as it runs, so they may build an
    index ``CONCURRENTLY``, and they read what the transaction made,
    whichever was registered first (user report D2). When an
    ``on_migrated`` hook fails, its transaction is rolled back and no
    follow-up runs.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'hooks.sqlite3'}"
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "followed"], DATABASES={"default": {"URL": url}}
    ):
        setup()
        domains = [hook.domain for hook in on_migrated_committed]
        notes = run_migrated_hooks("default")
        ran = list(calls)
    calls.clear()
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "breaking", "followed"],
        DATABASES={"default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'b.sqlite3'}"}},
    ):
        setup()
        with pytest.raises(RuntimeError, match="the hook failed"):
            run_migrated_hooks("default")
    asyncio.run(connections.close_all())

    assert domains == ["followed"]
    assert notes == ["followed_rows read"]  # For migrate to print.
    assert ran == [
        ("followed", "migrated", "default", None),
        ("followed", "built", "default", "AUTOCOMMIT", None),
    ]
    assert calls == []


@pytest.mark.usefixtures("database_access")
def test_a_failed_follow_up_fails_after_the_hooks_committed(
    calls: list[tuple[str, ...]], tmp_path: Path
) -> None:
    """The error reaches ``migrate`` as it is; what ``on_migrated`` made stays.

    ``on_migrated`` hooks fail so too. Run ``migrate`` again: both run again.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'hooks.sqlite3'}"
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "unfinished"], DATABASES={"default": {"URL": url}}
    ):
        setup()
        with pytest.raises(RuntimeError, match="the follow-up failed"):
            run_migrated_hooks("default")

        async def tables() -> list[str]:
            async with connections.engine().connect() as connection:
                return await connection.run_sync(
                    lambda sync: inspect(sync).get_table_names()
                )

        kept = asyncio.run(tables())
    asyncio.run(connections.close_all())

    assert kept == ["kept_rows"]


def test_tenant_follow_ups_are_refused_through_pgbouncer(
    calls: list[tuple[str, ...]],
) -> None:
    """Its transaction pooling keeps no ``search_path``: no hook runs at all.

    The follow-ups run outside a transaction, in the tenant's schema set for
    the session; PgBouncer would hand it to another client, or lose it. The
    schema owner connects directly with ``MIGRATION_URL``.
    """
    url = "postgresql+asyncpg://owner@pgbouncer.invalid:6432/app"
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "followed"],
        DATABASES={"default": {"URL": url, "PGBOUNCER": True}},
        TENANCY={"SCHEMA_DATABASES": ["default"]},
    ):
        setup()
        with pytest.raises(ImproperlyConfigured, match="set MIGRATION_URL"):
            run_migrated_hooks("default", tenant="acme")

    assert calls == []


def test_migrated_follow_ups_run_in_the_tenants_schema(
    calls: list[tuple[str, ...]], database_url: str
) -> None:
    """After ``migrate --tenant``, a follow-up works where the hooks did."""
    if not database_url.startswith("postgresql"):
        pytest.skip("schemas per tenant need PostgreSQL")
    schema = tenant_schema("acme")
    on_server(database_url, f'CREATE SCHEMA "{schema}"')
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "followed"],
        DATABASES={"default": {"URL": database_url}},
        TENANCY={"SCHEMA_DATABASES": ["default"]},
    ):
        setup()
        run_migrated_hooks("default", tenant="acme")
    asyncio.run(connections.close_all())

    assert calls == [
        ("followed", "migrated", "default", schema),
        ("followed", "built", "default", "AUTOCOMMIT", schema),
    ]


def test_hooks_are_the_installed_domains_and_forgotten_with_them(
    calls: list[tuple[str, ...]], tmp_path: Path
) -> None:
    """A registry cleared and loaded again holds only what its domains register."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'hooks.sqlite3'}"
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "typed"], DATABASES={"default": {"URL": url}}
    ):
        setup()
        first = on_stage.hooks()
        assert on_stage.hooks() is first  # Worked out once.
        registry.clear()
        assert list(on_stage) == []
        setup()
        assert [hook.domain for hook in on_stage] == ["typed", "typed"]
        registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "quiet"], DATABASES={"default": {"URL": url}}
    ):
        setup()
        assert list(on_stage) == []
        assert list(on_committed) == []


def test_hooks_register_in_ready_or_for_a_named_domain(
    calls: list[tuple[str, ...]],
) -> None:
    """Outside a domain's ``ready()``, the domain must be named."""
    point: HookPoint[Any] = HookPoint("probe")

    async def probe() -> None:
        """Do nothing."""

    with override_settings(INSTALLED_DOMAINS=[ALPHA, "quiet", "typed"]):
        setup()
        with pytest.raises(
            ImproperlyConfigured,
            match=r"^Register probe hooks in DomainConfig\.ready\(\); outside it, "
            r"once the domains are loaded, name an installed domain: "
            r"domain='label'\.$",
        ):
            point(probe)
        assert point(probe, domain="typed") is probe
        with pytest.raises(
            ImproperlyConfigured,
            match=r"^probe hook .*probe names domain 'elsewhere', which is not "
            r"loaded: register it in that domain's DomainConfig\.ready\(\), where "
            r"it is kept as long as the domain is installed\.$",
        ):
            point(probe, domain="elsewhere")  # It would never be called (SEC-12a-1).

        assert list(point) == [Hook("typed", probe)]
        assert repr(point) == "<HookPoint probe>"


@pytest.mark.usefixtures("calls")
def test_an_order_runs_a_hook_before_or_after_the_others() -> None:
    """Lowest order first, whatever its domain; within one, domain order.

    Approvals runs its hold of an admin change after the project's checks
    this way (user report A19). ``hooks_kept`` puts the orders back too.
    """
    point: HookPoint[Any] = HookPoint("ordered")

    async def first() -> None:
        """Run before every other."""

    async def plain() -> None:
        """Run in domain order."""

    async def typed() -> None:
        """Run in domain order, after alpha's."""

    async def last() -> None:
        """Run after every other."""

    async def later() -> None:
        """Run after the last, registered after it."""

    def names(hooks: Any) -> list[str]:  # noqa: ANN401 - hooks.
        return [hook.call.__name__ for hook in hooks]

    with override_settings(INSTALLED_DOMAINS=[ALPHA, "quiet", "typed"]):
        setup()
        point(last, domain="alpha", order=100)
        point(typed, domain="typed")
        point(first, domain="typed", order=-1)
        point(plain, domain="alpha")
        before = names(point)
        with hooks_kept():
            point(later, domain="typed", order=100)
            kept = names(point)
        after = names(point)
        refused = []
        for wrong in ("last", True, 1.5):
            with pytest.raises(ImproperlyConfigured, match="whole number") as raised:
                point(later, domain="alpha", order=wrong)  # type: ignore[arg-type]
            refused.append(str(raised.value))

        assert before == ["first", "plain", "typed", "last"]
        assert names(point.of("typed")) == ["first", "typed"]
        assert kept == ["first", "plain", "typed", "last", "later"]
        assert after == before
        assert refused[0].startswith("ordered hook ")
        assert refused[0].endswith("takes a whole number as its order, not 'last'.")


def test_a_points_own_checks_and_an_empty_point() -> None:
    """A point checks ``arguments`` and ``awaited``; one unused has no hooks."""
    setup()
    point: HookPoint[Any] = HookPoint("own", arguments=0, awaited=True)

    def plain() -> None:
        """Not awaitable."""

    assert list(point) == []
    with pytest.raises(ImproperlyConfigured, match=r"own hook .*plain must be an"):
        point(plain, domain="alpha")

    async def one(value: object) -> None:
        """Takes an argument the point does not give."""

    with pytest.raises(ImproperlyConfigured, match=r"must take 0 positional arguments"):
        point(one, domain="alpha")


@pytest.mark.usefixtures("calls")
def test_a_projects_own_point_checks_what_it_declares() -> None:
    """A point a test builds keeps its arguments and awaited.

    The framework's points are built when it is imported, before a mutation
    run picks a mutant, so only a point built here runs ``__init__``'s.
    """
    point: HookPoint[Any] = HookPoint("audited", arguments=1, awaited=True)

    async def one(record: object) -> None:
        """Take the record."""

    async def none() -> None:
        """Take nothing."""

    def plain(record: object) -> None:
        """Take the record, without awaiting."""

    with override_settings(INSTALLED_DOMAINS=[ALPHA, "quiet", "typed"]):
        setup()
        assert list(point) == []
        assert point(one, domain="typed") is one
        with pytest.raises(ImproperlyConfigured, match="must take 1 positional"):
            point(none, domain="typed")
        with pytest.raises(ImproperlyConfigured, match="must be an async function"):
            point(plain, domain="typed")
        assert list(point) == [Hook("typed", one)]

    assert (point.name, point.arguments, point.awaited) == ("audited", 1, True)


@pytest.mark.usefixtures("calls")
@pytest.mark.usefixtures("database_access")
def test_a_failed_follow_up_is_reported_with_its_domain(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The data is committed: the failure is logged and reported, not raised."""
    reports: list[dict[str, object]] = []
    monkeypatch.setattr(
        uow_module, "report_error", lambda exc, **context: reports.append(context)
    )
    url = f"sqlite+aiosqlite:///{tmp_path / 'hooks.sqlite3'}"
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "failing", "typed"],
        DATABASES={"default": {"URL": url}},
    ):
        setup()
        with caplog.at_level(logging.ERROR, "domainkit.db.uow"):
            commit()

    assert caplog.messages == ["After-commit work of 'failing' failed"]
    assert reports == [{"domain": "failing"}]


def test_contrib_apps_register_only_the_hooks_they_have() -> None:
    """Of the contrib apps, four join commits; a commit calls no other domain."""
    with override_settings(
        INSTALLED_DOMAINS=[*FULL_CONTRIB, ALPHA],
        LEDGERS={"evidence": {"URL": "memory://"}},
    ):
        setup()
        stage = [hook.domain for hook in on_stage]
        committed = [hook.domain for hook in on_committed]
        staged = [hook.domain for hook in on_stage_transaction]
        followed = [hook.domain for hook in on_transaction_committed]
        migrated = [hook.domain for hook in on_migrated]
        built = [hook.domain for hook in on_migrated_committed]
        installed = len(registry)

    assert installed == 13
    assert stage == ["auth", "audit", "messaging", "idempotency"]
    assert committed == ["auth", "messaging", "idempotency"]
    assert staged == followed == ["idempotency"]
    assert migrated == ["contenttypes", "auth", "audit", "audit"]
    assert built == ["auth"]  # The user lookups' indexes, concurrently.


def test_hooks_worked_out_while_the_domains_change_are_not_kept(
    calls: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A list made while ``populate`` or ``clear`` ran is returned, not kept."""
    with override_settings(INSTALLED_DOMAINS=[ALPHA, "typed"]):
        setup()
        collect = on_stage._collect

        def changing(registry: Any, registered: Any) -> Any:  # noqa: ANN401 - the point's own.
            found = list(collect(registry, registered))
            registry.generation += 1  # The domains changed meanwhile.
            return iter(found)

        monkeypatch.setattr(on_stage, "_collect", changing)
        first = on_stage.hooks()
        monkeypatch.setattr(on_stage, "_collect", collect)

        assert [hook.domain for hook in first] == ["typed", "typed"]
        assert on_stage._hooks is None  # Not kept: worked out again next time.
        assert on_stage.hooks() == ()  # The stale registrations are forgotten.
