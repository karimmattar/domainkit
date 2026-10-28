"""SEC-12a-1: a hook registered with ``domain=`` outside ``ready()`` is dropped.

A point refuses a registration outside a domain's ``ready()`` with "Register
erase_subject hooks in DomainConfig.ready(), or name the domain:
domain='label'." A domain that takes the second way, at the top of its
``apps`` module (or any module ``setup()`` imports before the domains are
ready), registers for the registry's generation of that moment.
``populate`` then counts a change (``_changed``) before any ``ready()``
runs, and the registration is forgotten, silently: the domain's erasure
hook is never called, and ``privacy erase`` reports success while the
copies it keeps stay. The registration must be kept, or refused.
"""

from domainkit import setup
from domainkit.apps import registry
from domainkit.apps.hooks import on_erase_subject
from domainkit.conf import override_settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import ALPHA, MakePackage

KEEPER = """
from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_erase_subject


async def erase_copies(subject, reason):
    return {"copies": 1}


# As the refusal says: outside ready(), name the domain.
on_erase_subject(erase_copies, domain="keeper")


class KeeperConfig(DomainConfig):
    pass
"""


def test_a_named_registration_before_the_domains_load_is_kept_or_refused(
    make_package: MakePackage,
) -> None:
    """The erasure hook runs, or ``setup()`` says it never will."""
    make_package("keeper", {"apps.py": KEEPER})
    registry.clear()
    with override_settings(INSTALLED_DOMAINS=[ALPHA, "keeper"]):
        try:
            setup()
        except ImproperlyConfigured:
            return  # Refused, loudly: fixed.
        erasers = [hook.call.__name__ for hook in on_erase_subject.of("keeper")]
    registry.clear()
    assert erasers == ["erase_copies"], (
        "keeper's erasure hook was accepted, then forgotten: privacy erase "
        "would never call it"
    )
