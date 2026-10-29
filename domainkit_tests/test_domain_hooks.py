"""A domain's life as typed hooks: startup, checks, seeds, erasure (DSGN-03).

Each hook method of ``DomainConfig`` became a point a domain registers on
in ``ready()``. A mistake in a registered hook fails ``setup()``, naming
the domain; so does a hook method 0.13 removed, still defined.
"""

import asyncio
import functools
from typing import Any

import pytest

from domainkit import setup
from domainkit.apps import DomainConfig, DomainRegistry, registry
from domainkit.apps import hooks as hooks_module
from domainkit.apps.config import REMOVED_HOOKS
from domainkit.apps.hooks import (
    HookPoint,
    on_check,
    on_check_application,
    on_deployment_warnings,
    on_erase_subject,
    on_seed,
    on_startup,
)
from domainkit.checks import CheckFailed, CheckMessage
from domainkit.conf import override_settings
from domainkit.db import hooks as db_hooks
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import ALPHA, MakePackage
from domainkit_tests.sample_domains.alpha.apps import AlphaConfig

MISSPELT = """
from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_erase_subject


class MisspeltConfig(DomainConfig):
    def ready(self):
        on_erase_subject(self.erase_subjects)

    async def erase_subjects(self, subject):
        return {}
"""
SYNC_CHECK = """
from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_check


class SyncConfig(DomainConfig):
    def ready(self):
        on_check(self.probe)

    def probe(self):
        pass
"""
OLD_STYLE = """
from domainkit.apps import DomainConfig
from oldlog import CALLS


class OldStyleConfig(DomainConfig):
    async def startup(self):
        CALLS.append("startup")

    async def shutdown(self):
        CALLS.append("shutdown")

    async def check(self):
        CALLS.append("check")

    async def seed(self):
        CALLS.append("seed")

    async def erase_subject(self, subject, reason):
        return {"copies": 1}

    async def export_subject(self, subject):
        return {"copies": [subject]}

    def check_application(self, app):
        return ["old problem"]

    def deployment_warnings(self):
        return ["old warning"]

    async def stage(self, uow, events, tasks):
        CALLS.append("stage")

    async def committed(self, uow):
        CALLS.append("committed")

    async def stage_transaction(self, connection, database):
        CALLS.append("stage_transaction")

    async def transaction_committed(self, database):
        CALLS.append("transaction_committed")

    async def migrated(self, connection, database):
        CALLS.append("migrated")
"""
INHERITED = """
from oldstyle.apps import OldStyleConfig


class ChildConfig(OldStyleConfig):
    pass
"""
ODD = """
from domainkit.apps import DomainConfig


class Probe:
    async def __call__(self):
        return None


class OddConfig(DomainConfig):
    check = Probe()
"""
BORROWED = """
from domainkit.apps import DomainConfig

from borrowed import hooks


class BorrowedConfig(DomainConfig):
    startup = hooks.startup
"""
SHAPES = """
from domainkit.apps import DomainConfig


class Seeding:
    async def seed(self):
        return None


class ShapesConfig(Seeding, DomainConfig):
    check = property(lambda self: None)
    migrated = staticmethod(lambda connection, database: None)

    def __init__(self, name, module):
        super().__init__(name, module)
        self.shutdown = self.close

    async def close(self):
        return None


async def erase(self, subject, reason):
    return {}


ShapesConfig.erase_subject = erase
"""
ERASING = """
from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_erase_subject

ERASED = []


async def erase_copies(subject, reason):
    ERASED.append(subject)
    return {"copies": 1}


class ErasingConfig(DomainConfig):
    def ready(self):
        on_erase_subject(erase_copies)
"""
TWICE = """
from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_check, on_seed, on_shutdown, on_startup
from twicelog import CALLS


class TwiceConfig(DomainConfig):
    def ready(self):
        on_startup(self.open_first)
        on_startup(self.open_second)
        on_shutdown(self.close)
        on_check(self.first)
        on_check(self.second)
        on_seed(self.sow)
        on_seed(self.sow_again)

    async def open_first(self):
        CALLS.append("open first")

    async def open_second(self):
        CALLS.append("open second")

    async def close(self):
        CALLS.append("close")

    async def first(self):
        CALLS.append("check first")

    async def second(self):
        CALLS.append("check second")
        if CALLS.count("check second") > 1:
            raise RuntimeError("the second check fails")

    async def sow(self):
        CALLS.append("seed")

    async def sow_again(self):
        CALLS.append("seed again")
"""
CLOSING = """
from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_shutdown
from twicelog import CALLS


class ClosingConfig(DomainConfig):
    def ready(self):
        on_shutdown(self.close)

    async def close(self):
        CALLS.append("closing closed")
"""


@pytest.fixture
def old_calls(make_package: MakePackage) -> list[str]:
    """What the method-style domain did: nothing, ever, from 0.13."""
    make_package("oldlog", {"__init__.py": "CALLS = []\n"})
    make_package("oldstyle", {"apps.py": OLD_STYLE})
    make_package("childstyle", {"apps.py": INHERITED})
    import oldlog  # type: ignore[import-not-found]

    return oldlog.CALLS  # type: ignore[no-any-return]


@pytest.fixture
def twice_calls(make_package: MakePackage) -> list[str]:
    """What the domains with several hooks of a kind did."""
    make_package("twicelog", {"__init__.py": "CALLS = []\n"})
    make_package("twice", {"apps.py": TWICE})
    make_package("closing", {"apps.py": CLOSING})
    make_package("bare", {})
    import twicelog  # type: ignore[import-not-found]

    return twicelog.CALLS  # type: ignore[no-any-return]


def test_a_misspelt_or_mistyped_hook_fails_setup(make_package: MakePackage) -> None:
    """It used to be called never, or fail each probe; now ``setup()`` refuses."""
    make_package("misspelt", {"apps.py": MISSPELT})
    make_package("synccheck", {"apps.py": SYNC_CHECK})

    with (
        override_settings(INSTALLED_DOMAINS=[ALPHA, "misspelt"]),
        pytest.raises(ImproperlyConfigured) as wrong,
    ):
        setup()
    assert str(wrong.value).startswith(
        "misspelt's erase_subject hook MisspeltConfig.erase_subjects must take "
        "2 positional arguments ("
    )
    registry.clear()
    with (
        override_settings(INSTALLED_DOMAINS=[ALPHA, "synccheck"]),
        pytest.raises(
            ImproperlyConfigured,
            match=r"^synccheck's check hook SyncConfig.probe must be an async "
            r"function\.$",
        ),
    ):
        setup()


def test_registrations_are_checked_against_the_point() -> None:
    """Arguments, and awaiting, as the point calls its hooks."""
    setup()

    async def one(app: object) -> None:
        """Awaitable where the point is not."""

    def anything(*args: object) -> list[str]:
        """Takes any arguments."""
        return []

    class Probe:
        async def __call__(self) -> None:
            """An object whose call is a coroutine."""

    wrong: Any = one  # What type checkers would refuse, tried at run time.
    with pytest.raises(ImproperlyConfigured, match=r"must be a plain function\.$"):
        on_check_application(wrong, domain="alpha")
    wrong = lambda: None  # noqa: E731 - named to show in the error.
    with pytest.raises(
        ImproperlyConfigured,
        match=r"^alpha's committed hook .*<lambda> must take 1 positional argument \(",
    ):
        db_hooks.on_committed(wrong, domain="alpha")
    wrong = len
    with pytest.raises(ImproperlyConfigured, match=r"must take 0 positional arguments"):
        on_deployment_warnings(wrong, domain="alpha")
    assert on_check_application(anything, domain="alpha") is anything
    probe = Probe()
    assert on_check(probe, domain="alpha") is probe
    with pytest.raises(ImproperlyConfigured) as unnamed:
        on_erase_subject(probe, domain="alpha")  # type: ignore[arg-type]
    assert str(unnamed.value).startswith(
        f"alpha's erase_subject hook {probe!r} must take 2 positional arguments ("
    )
    unsigned: Any = max  # A built-in without a signature is trusted.
    assert on_deployment_warnings(unsigned, domain="alpha") is unsigned
    free: HookPoint[Any] = HookPoint("free")
    assert free(one, domain="alpha") is one  # Nothing to check.


def refusal(domains: list[str]) -> CheckFailed:
    """What ``setup()`` raises with ``domains`` installed after alpha."""
    with (
        override_settings(INSTALLED_DOMAINS=[ALPHA, *domains]),
        pytest.raises(CheckFailed) as refused,
    ):
        setup()
    return refused.value


def test_a_removed_hook_method_fails_setup_naming_its_point(
    old_calls: list[str],
) -> None:
    """Every one of the 13 is named, with its point, before any ``ready()`` runs.

    0.12 called an override and warned of it; 0.13 calls none, so one
    left behind would be silently skipped: an ``erase_subject`` no one
    calls keeps a person's data through an erasure that reports success.
    """
    AlphaConfig.calls.clear()

    refused = refusal(["oldstyle"])

    assert str(refused) == "Installed domains define removed hook methods:\n" + (
        "\n".join(
            f"  apps.E002: Domain 'oldstyle': OldStyleConfig.{method} is a hook "
            "method, which domainkit 0.13 removed; nothing would call it.\n"
            f"    Hint: Register the function with {point}(...) in ready(), "
            "under a name of its own."
            for method, point in REMOVED_HOOKS.items()
        )
    )
    assert [
        message.id for message in refused.messages if isinstance(message, CheckMessage)
    ] == ["apps.E002"] * 13
    assert AlphaConfig.calls == []  # No domain was made ready.
    assert not registry.ready
    assert old_calls == []


def test_every_domain_that_defines_one_is_named_in_domain_order(
    old_calls: list[str], make_package: MakePackage
) -> None:
    """An inherited method names the class that defines it, and each domain."""
    make_package("odd", {"apps.py": ODD})

    refused = refusal(["odd", "childstyle"])

    assert [message.partition(" is ")[0] for message in refused.messages] == [
        "Domain 'odd': OddConfig.check",
        *(f"Domain 'childstyle': OldStyleConfig.{method}" for method in REMOVED_HOOKS),
    ]


def test_a_name_is_found_however_the_domain_defines_it(
    make_package: MakePackage,
) -> None:
    """A base, a property, a staticmethod, a later assignment, the instance.

    A function assigned from another module counts as a method written in
    the class does.
    """
    make_package("shapes", {"apps.py": SHAPES})
    make_package(
        "borrowed",
        {
            "hooks.py": "async def startup(self):\n    return None\n",
            "apps.py": BORROWED,
        },
    )

    refused = refusal(["shapes", "borrowed"])

    assert [message.partition(" is ")[0] for message in refused.messages] == [
        "Domain 'shapes': ShapesConfig.shutdown",  # On the instance.
        "Domain 'shapes': ShapesConfig.check",  # A property.
        "Domain 'shapes': Seeding.seed",  # A base that is no DomainConfig.
        "Domain 'shapes': ShapesConfig.erase_subject",  # Set after the class.
        "Domain 'shapes': ShapesConfig.migrated",  # A staticmethod.
        "Domain 'borrowed': BorrowedConfig.startup",
    ]


@pytest.mark.anyio
async def test_a_domain_that_registers_instead_is_called(
    make_package: MakePackage,
) -> None:
    """``on_erase_subject`` in ``ready()``: the way that works."""
    make_package("erasing", {"apps.py": ERASING})
    with override_settings(INSTALLED_DOMAINS=[ALPHA, "erasing"]):
        setup()
        answers = [
            await hook.call("subject-1", "request")
            for hook in on_erase_subject.of("erasing")
        ]
    from erasing.apps import ERASED  # type: ignore[import-not-found]

    assert (answers, ERASED) == ([{"copies": 1}], ["subject-1"])


def test_the_base_class_defines_no_hook_method() -> None:
    """A domain that defines none loads; ``databases`` and ``holds_table`` stay."""
    for method in REMOVED_HOOKS:
        assert not hasattr(DomainConfig, method)
    setup()

    assert [hook.domain for hook in on_check] == ["alpha"]
    assert callable(DomainConfig.databases)
    assert callable(DomainConfig.holds_table)


@pytest.mark.usefixtures("twice_calls")
def test_hooks_register_for_the_registry_that_loads_the_domains(
    twice_calls: list[str],
) -> None:
    """A registry of one's own keeps its hooks; the process's sees none of them."""
    setup()
    own = DomainRegistry()
    own.populate([ALPHA, "twice", "bare"])

    assert [hook.domain for hook in on_startup.hooks(own)] == [
        "alpha",
        "twice",
        "twice",
    ]
    assert [hook.domain for hook in on_startup] == ["alpha"]
    assert on_check.of("twice", own) == tuple(
        hook for hook in on_check.hooks(own) if hook.domain == "twice"
    )
    assert on_check.of("twice") == ()


def test_a_domain_starts_with_all_its_hooks_and_stops_with_its_own(
    twice_calls: list[str],
) -> None:
    """Domains without hooks are skipped; one with only shutdown hooks stops."""
    own = DomainRegistry()
    own.populate([ALPHA, "bare", "twice", "closing"])

    async def live() -> None:
        async with own.lifespan(stop_within=5):
            twice_calls.append("serving")

    asyncio.run(live())

    assert twice_calls == [
        "open first",
        "open second",
        "serving",
        "closing closed",
        "close",
    ]
    assert AlphaConfig.calls == ["ready", "startup", "shutdown"]


def test_readiness_runs_a_domains_checks_in_turn(
    twice_calls: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """One label per failing domain; domains without checks cost nothing."""
    own = DomainRegistry()
    own.populate([ALPHA, "bare", "twice"])

    assert asyncio.run(own.check(1.0)) == []
    assert asyncio.run(own.check(1.0)) == ["twice"]
    assert twice_calls == ["check first", "check second"] * 2
    assert "Readiness check 'twice' failed" in caplog.text
    assert asyncio.run(DomainRegistry().check(1.0)) == []


def test_seed_skips_the_domains_not_named(twice_calls: list[str]) -> None:
    """A domain left out is skipped; those after it are seeded."""
    from domainkit.management import call_command

    with override_settings(INSTALLED_DOMAINS=[ALPHA, "bare", "twice"]):
        setup()
        on_seed(functools.partial(asyncio.sleep, 0), domain="alpha")
        call_command("seed", "twice")

    assert twice_calls == ["seed", "seed again"]


def test_seed_runs_every_hook_and_names_each_domain_once(
    twice_calls: list[str],
) -> None:
    """``seed`` runs the hooks of the domains named, in order."""
    from domainkit.management import call_command

    with override_settings(INSTALLED_DOMAINS=[ALPHA, "bare", "twice"]):
        setup()
        call_command("seed")
        call_command("seed", "alpha")

    assert twice_calls == ["seed", "seed again"]


def test_every_hook_method_has_its_point() -> None:
    """Each removed method names the point of the same name, and only those."""
    points = {
        point.name: f"{module.__name__}.{name}"
        for module in (hooks_module, db_hooks)
        for name, point in vars(module).items()
        if isinstance(point, HookPoint)
    }

    assert points == REMOVED_HOOKS
    assert all(
        name == f"on_{method}"
        for method, name in (
            (method, target.rpartition(".")[2]) for method, target in points.items()
        )
    )


def test_registrations_made_while_a_registry_changes_are_forgotten() -> None:
    """A registry loaded again starts with no registrations from before."""
    setup()
    on_seed(functools.partial(asyncio.sleep, 0), domain="alpha")
    assert [hook.domain for hook in on_seed] == ["alpha"]
    registry.clear()
    setup()
    assert list(on_seed) == []
