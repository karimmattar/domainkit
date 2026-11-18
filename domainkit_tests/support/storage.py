"""Storages the tests make from the settings, forgotten around each test."""

from collections.abc import Iterator

import pytest

from domainkit.storage import storages


@pytest.fixture
def fresh_storages() -> Iterator[None]:
    """Forget the storages made."""
    storages.reset()
    yield
    storages.reset()
