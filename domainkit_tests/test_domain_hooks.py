"""A domain's life as typed hooks: startup, checks, seeds, erasure (DSGN-03).

Each hook method of ``DomainConfig`` became a point a domain registers on
in ``ready()``. A mistake in a registered hook fails ``setup()``, naming
the domain; a method still overridden keeps working, warned of once.
"""

import asyncio
import functools
import warnings
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI

from domainkit import setup
from domainkit.apps import DomainConfig, DomainRegistry, registry
from domainkit.apps import hooks as hooks_module
from domainkit.apps.config import REPLACED_HOOKS
from domainkit.apps.hooks import (
    HookPoint,
    on_check,
    on_check_application,
    on_deployment_warnings,
    on_erase_subject,
    on_export_subject,
    on_seed,
    on_startup,
)
from domainkit.conf import override_settings
from domainkit.db import hooks as db_hooks
from domainkit.deprecation import DomainkitDeprecationWarning
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


class Inheriting(OldStyleConfig):
    pass
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
    """What the method-style domain did."""
    make_package("oldlog", {"__init__.py": "CALLS = []\n"})
    make_package("oldstyle", {"apps.py": OLD_STYLE.replace("\n\n\nclass Inh", "\n#")})
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


@pytest.fixture
def loud() -> Iterator[None]:
    """Show deprecation warnings by the default filter, once per place."""
    with warnings.catch_warnings():
        warnings.simplefilter("default", DomainkitDeprecationWarning)
        yield


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


def test_old_hook_methods_still_work_and_warn_once_at_their_line(
    old_calls: list[str], loud: None
) -> None:
    """Every hook method is still called, warned of where it is defined."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default", DomainkitDeprecationWarning)
        with override_settings(INSTALLED_DOMAINS=[ALPHA, "oldstyle"]):
            setup()
            registry.clear()
            setup()  # Loaded again: not warned again.
            assert asyncio.run(registry.check(1.0)) == []

            async def live() -> None:
                async with registry.lifespan():
                    pass

            asyncio.run(live())
            assert [hook.call(FastAPI()) for hook in on_check_application] == [
                ["old problem"]
            ]
            assert [hook.call() for hook in on_deployment_warnings] == [["old warning"]]
            assert [hook.domain for hook in on_seed] == ["oldstyle"]
            assert [hook.domain for hook in on_erase_subject] == ["oldstyle"]
            assert [hook.domain for hook in on_export_subject] == ["oldstyle"]

    assert old_calls == ["check", "startup", "shutdown"]
    found = sorted(
        (str(warning.message), warning.lineno)
        for warning in caught
        if warning.category is DomainkitDeprecationWarning
    )
    methods = [
        "check",
        "check_application",
        "deployment_warnings",
        "erase_subject",
        "export_subject",
        "seed",
        "shutdown",
        "startup",
    ]
    assert [message for message, _ in found] == [
        f"Domain 'oldstyle': OldStyleConfig.{method} is a deprecated hook method; "
        f"register the function with {REPLACED_HOOKS[method]}(...) in ready(). "
        "Hook methods are removed in domainkit 0.13."
        for method in methods
    ]
    assert {warning.filename for warning in caught} == {
        str(__import__("oldstyle.apps").apps.__file__)
    }
    assert len({line for _, line in found}) == len(methods)


def test_the_warning_is_the_projects_own_module_s(old_calls: list[str]) -> None:
    """A filter on the project's module catches it, as ``filterwarnings`` would."""
    with (
        override_settings(INSTALLED_DOMAINS=[ALPHA, "oldstyle"]),
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("ignore", DomainkitDeprecationWarning)
        warnings.filterwarnings(
            "error", category=DomainkitDeprecationWarning, module=r"oldstyle\.apps$"
        )
        with pytest.raises(DomainkitDeprecationWarning, match="'oldstyle'"):
            setup()


def test_a_hook_method_defined_elsewhere_is_warned_where_it_is(
    make_package: MakePackage,
) -> None:
    """A function assigned as the method is warned of at its own line."""
    make_package(
        "borrowed",
        {
            "hooks.py": "async def startup(self):\n    return None\n",
            "apps.py": BORROWED,
        },
    )

    with (
        override_settings(INSTALLED_DOMAINS=[ALPHA, "borrowed"]),
        pytest.warns(
            DomainkitDeprecationWarning, match="BorrowedConfig.startup"
        ) as caught,
    ):
        setup()

    assert (caught[0].filename, caught[0].lineno) == (
        __import__("borrowed.hooks").hooks.__file__,
        1,
    )


def test_an_inherited_hook_method_names_the_class_that_defines_it(
    old_calls: list[str],
) -> None:
    """The warning names the domain loaded and the class with the method."""
    with (
        override_settings(INSTALLED_DOMAINS=[ALPHA, "childstyle"]),
        pytest.warns(DomainkitDeprecationWarning) as caught,
    ):
        setup()

    assert str(caught[0].message).startswith(
        "Domain 'childstyle': OldStyleConfig.startup is a deprecated hook method"
    )


def test_a_hook_method_that_is_no_function_is_warned_at_its_module(
    make_package: MakePackage,
) -> None:
    """Without a line of its own, the warning points at its module's top."""
    make_package("odd", {"apps.py": ODD})

    with (
        override_settings(INSTALLED_DOMAINS=[ALPHA, "odd"]),
        pytest.warns(DomainkitDeprecationWarning, match="OddConfig.check") as caught,
    ):
        setup()

    assert (caught[0].filename, caught[0].lineno) == (
        __import__("odd.apps").apps.__file__,
        1,
    )
    assert [hook.domain for hook in on_check] == ["alpha", "odd"]


def test_the_base_methods_are_deprecated_too() -> None:
    """Each says what to use instead, to type checkers and when called."""
    for method, replacement in REPLACED_HOOKS.items():
        assert replacement in getattr(DomainConfig, method).__deprecated__
    domain = DomainConfig("plain", __import__("domainkit_tests"))
    with pytest.warns(DomainkitDeprecationWarning, match="on_check_application"):
        assert domain.check_application(FastAPI()) == []
    with pytest.warns(DomainkitDeprecationWarning, match="on_deployment_warnings"):
        assert domain.deployment_warnings() == []


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
    """The deprecated methods and the points' ``method`` agree."""
    points = {
        point.method: f"{module.__name__}.{name}"
        for module in (hooks_module, db_hooks)
        for name, point in vars(module).items()
        if isinstance(point, HookPoint)
    }

    assert points == REPLACED_HOOKS
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
