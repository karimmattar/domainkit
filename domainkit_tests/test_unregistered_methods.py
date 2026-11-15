"""A ``start`` or ``stop`` method nobody registered is named (user report C1).

Since 0.13 only the functions a domain registers run. A project wrote
``async def start(self)`` on its ``DomainConfig``, as domainkit's own
audit and keys apps write theirs, and never registered it: the check it
made at startup never ran, in the API or the worker, without a word.
"""

from collections.abc import Iterator

import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.apps.hooks import unregistered_methods
from domainkit.checks import CheckMessage
from domainkit.conf import override_settings
from domainkit.management import call_command
from domainkit_tests.conftest import ALPHA, MakePackage

FORGOTTEN = """
from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_shutdown


class ForgottenConfig(DomainConfig):
    def ready(self):
        on_shutdown(self.stop)

    async def start(self):
        raise RuntimeError("the audit trail is not append-only")

    async def stop(self):
        pass
"""

KEPT = """
from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_startup


class KeptConfig(DomainConfig):
    def ready(self):
        on_startup(self.start)

    async def start(self):
        pass

    def stop(self):
        '''A plain helper of that name: no point would await it.'''
"""


@pytest.fixture
def installed(make_package: MakePackage) -> Iterator[None]:
    """A domain that forgot its ``start``, and one that registered it."""
    make_package("forgotten", {"apps.py": FORGOTTEN})
    make_package("kept", {"apps.py": KEPT})
    registry.clear()
    with override_settings(INSTALLED_DOMAINS=[ALPHA, "forgotten", "kept"]):
        setup()
        yield
    registry.clear()


@pytest.mark.usefixtures("installed")
def test_a_coroutine_method_no_point_holds_is_named() -> None:
    """``start`` of the first; its registered ``stop``, and the second, are fine."""
    (found,) = unregistered_methods()

    assert isinstance(found, CheckMessage)
    assert found.id == "apps.W001"
    assert found.message == (
        "Domain 'forgotten': ForgottenConfig.start is a coroutine method that no "
        "hook point holds, so nothing calls it."
    )
    assert found.hint == (
        "Register it in ready(): on_startup(self.start) or on_shutdown(self.start); "
        "or name a helper otherwise."
    )


@pytest.mark.usefixtures("installed")
def test_check_lists_it_in_every_environment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A warning of ``check``, as ``check --deploy`` refuses any."""
    call_command("check")

    assert "apps.W001: Domain 'forgotten'" in capsys.readouterr().out


def test_domainkits_own_apps_register_theirs() -> None:
    """Audit and keys define ``start`` and register it."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "domainkit.contrib.audit", "domainkit.contrib.keys"]
    ):
        setup()
        try:
            assert unregistered_methods() == []
        finally:
            registry.clear()
