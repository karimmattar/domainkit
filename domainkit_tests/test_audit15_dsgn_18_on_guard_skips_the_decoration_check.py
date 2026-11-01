"""DSGN-18: ``on_guard`` is a second, unchecked way to register a guard.

``guard(action, service, ...)`` refuses a service that is not decorated
``@requires_approval(action)``: without the decoration, calling the service
directly skips approval. ``on_guard``, public beside it and documented as
``on_guard(Guard(...))``, registers the same ``Guard`` without that check,
so the guard is listed, its policy shown, and the service still runs
unapproved when called directly.
"""

from collections.abc import Iterator
from typing import Final

import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.approvals import Guard, Policy, guards
from domainkit.contrib.approvals.guards import on_guard
from domainkit.ddd import Command
from domainkit.exceptions import ImproperlyConfigured
from domainkit.money import Money

ALPHA: Final = "domainkit_tests.sample_domains.alpha"
DOMAINS: Final = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.audit",
    "domainkit.contrib.approvals",
    ALPHA,
]
POLICY: Final = Policy("tests.payouts", approvals=2, permission="tests.approve")


class Payout(Command):
    """A payout: the command of a use case meant to be guarded."""

    amount: Money


async def payout(command: Payout, uow: object) -> str:
    """Not decorated ``@requires_approval``: a direct call is never refused."""
    return "paid"


@pytest.fixture
def installed() -> Iterator[None]:
    """Messaging, audit, approvals, and a sample domain, without a database."""
    registry.clear()
    with override_settings(INSTALLED_DOMAINS=DOMAINS):
        setup()
        yield
    registry.clear()


def test_on_guard_refuses_a_service_not_decorated_for_its_action(
    installed: None,
) -> None:
    """Registering through the hook point checks what ``guard()`` checks."""
    with pytest.raises(ImproperlyConfigured, match="requires_approval"):
        on_guard(
            Guard("tests.payout", Payout, payout, object, (POLICY,)), domain="alpha"
        )
    assert "tests.payout" not in guards()
