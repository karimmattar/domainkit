"""PERF-24: every translated message builds a path for each installed domain.

``domainkit.i18n.translation.gettext`` calls ``catalog()``, which calls
``locale_dirs()``: a new ``Path`` per installed domain
(``config.path / "locale"``), every time, and then hashes and compares
that tuple of new paths to find the cached catalog (``_catalog``'s
``@cache``). Each refused request (401, 403, 422, 429) translates its
message, so it pays for it: measured, one ``gettext`` is 213 calls and
34 us with 3 domains, 772 calls and 116 us with the fintech profile's
12; a fintech 401 costs 1,537 calls, 568 more than the default
template's, nearly all of them this. It grows with every domain a
project adds, on the paths a credential-stuffing or rate-limited client
drives.

The right behaviour: the directories are worked out once per registry
and settings (as ``registry.settings`` is, PERF-06), so a message costs
the same whatever the number of domains.
"""

from collections.abc import Iterator
from typing import Final

import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.apps.config import DomainConfig
from domainkit.conf import override_settings
from domainkit.i18n import gettext
from domainkit_tests.conftest import ALPHA

MESSAGES: Final = 100


@pytest.fixture
def domains() -> Iterator[int]:
    """A few domains installed; how many."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.contenttypes",
            "domainkit.contrib.auth",
            ALPHA,
        ]
    ):
        setup()
        yield len(list(registry))
    registry.clear()


def test_translating_messages_does_not_walk_the_domains_each_time(
    domains: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hundred messages read each domain's directory once, not a hundred times."""
    gettext("Invalid token.")  # The catalog is loaded once, as it may be.
    reads = 0
    path = DomainConfig.path

    def counted(self: DomainConfig) -> object:
        nonlocal reads
        reads += 1
        return path.fget(self)  # type: ignore[attr-defined]

    monkeypatch.setattr(DomainConfig, "path", property(counted))
    for _ in range(MESSAGES):
        gettext("Invalid token.")
    assert reads <= domains, f"{reads} reads of domain paths for {MESSAGES} messages"
