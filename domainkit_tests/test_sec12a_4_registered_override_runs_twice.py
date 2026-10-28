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
"""

import warnings

import pytest

from domainkit.apps import DomainRegistry
from domainkit.apps.hooks import on_erase_subject
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.exceptions import ImproperlyConfigured
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


@pytest.mark.anyio
async def test_a_registered_override_is_called_once(make_package: MakePackage) -> None:
    """One start, one erasure, one delivery counted."""
    make_package("dual", {"apps.py": DUAL})
    own = DomainRegistry()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DomainkitDeprecationWarning)
        try:
            own.populate([ALPHA, "dual"])
        except ImproperlyConfigured:
            return  # Refused, loudly: fixed.
    from dual.apps import CALLS  # type: ignore[import-not-found]

    async with own.lifespan():
        pass
    counted = 0
    for hook in on_erase_subject.of("dual", own):  # As privacy sums them.
        counted += (await hook.call("subject-1", "request"))["deliveries"]

    assert (CALLS, counted) == (["startup", "erase"], 1)
