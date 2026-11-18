"""The key ring and the sample cards domain, for the encrypted fields' tests.

``bank`` installs both, with the alpha domain, in a database of the test's
own, and migrates it; a test file takes it with ``from
domainkit_tests.support.keys import bank``.
"""

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.management import call_command
from domainkit_tests.conftest import ALPHA

KEYS: Final = "domainkit.contrib.keys"
CARDS: Final = "domainkit_tests.sample_domains.cards"


@pytest.fixture
def bank(database_url: str, tmp_path: Path) -> Iterator[str]:
    """The cards domain and the key ring, migrated."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[KEYS, CARDS, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        call_command("makemigrations", "-m", "bank", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield database_url
    registry.clear()
