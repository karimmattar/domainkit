"""Typed hooks: a domain registers what it takes part in, and only it is called."""

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit import setup
from domainkit.apps import registry
from domainkit.apps.hooks import Hook, HookPoint
from domainkit.conf import override_settings
from domainkit.db import SqlAlchemyUnitOfWork, connections
from domainkit.db import uow as uow_module
from domainkit.db.hooks import (
    on_committed,
    on_migrated,
    on_stage,
    on_stage_transaction,
    on_transaction_committed,
)
from domainkit.db.migrations import run_migrated_hooks
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import ALPHA, MakePackage

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
        installed = len(registry)

    assert installed == 13
    assert stage == ["auth", "audit", "messaging", "idempotency"]
    assert committed == ["auth", "messaging", "idempotency"]
    assert staged == followed == ["idempotency"]
    assert migrated == ["contenttypes", "auth"]


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
