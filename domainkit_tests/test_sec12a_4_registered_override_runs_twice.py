"""SEC-12a-4: a hook method a domain also registers runs twice.

Loading a domain that overrides a deprecated hook method warns: "register
the function with domainkit.apps.hooks.on_startup(...) in ready()". The
shortest move that does so keeps the method and registers it,
``on_startup(self.startup)``; the warning goes on (the method is still an
override), and the point calls the domain's registered hooks, then its
override (``HookPoint._collect``): the same bound method, twice. Every
start opens the domain's clients and pools twice (one is never closed);
every erasure runs the domain's ``erase_subject`` twice, and ``privacy``
sums both answers into the one ``privacy.copies_erased`` record, so the
audit trail says twice the copies were erased that were; and so on for
each point, the SQL ones too (``on_stage(self.stage)``: two outbox or
audit rows per commit). An override that is registered already must be
called once, or refused.

0.13 removed the hook methods and refuses a domain that still defines
one (``apps.E002``), registered or not: the method's name is the mistake,
so the domain never loads, and nothing runs twice.
"""

import pytest

from domainkit.apps import DomainRegistry
from domainkit.checks import CheckFailed, CheckMessage
from domainkit_tests.conftest import ALPHA, MakePackage

DUAL = """
from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_erase_subject, on_startup

CALLS = []


class DualConfig(DomainConfig):
    def ready(self):
        # As the warning says: register the function in ready().
        on_startup(self.startup)
        on_erase_subject(self.erase_subject)

    async def startup(self):
        CALLS.append("startup")

    async def erase_subject(self, subject, reason):
        CALLS.append("erase")
        return {"deliveries": 1}
"""


def test_a_registered_override_is_refused(make_package: MakePackage) -> None:
    """Refused before ``ready()``: nothing started, nothing erased or counted."""
    make_package("dual", {"apps.py": DUAL})
    own = DomainRegistry()

    with pytest.raises(CheckFailed) as refused:
        own.populate([ALPHA, "dual"])
    from dual.apps import CALLS  # type: ignore[import-not-found]

    assert [
        message.id
        for message in refused.value.messages
        if isinstance(message, CheckMessage)
    ] == ["apps.E002"] * 2
    assert "DualConfig.startup is a hook method" in str(refused.value)
    assert "DualConfig.erase_subject is a hook method" in str(refused.value)
    assert (CALLS, own.ready) == ([], False)
