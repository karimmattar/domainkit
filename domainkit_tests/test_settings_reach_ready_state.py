"""State built in ``ready()`` follows the settings a test changes (TEST-15).

The key ring is the one ``KEYS`` describes now (``SettingsKeyring``),
made once per value and found again when the settings are put back; the
approvals guard reads ``ENTRY_THRESHOLDS`` of the settings now.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import SecretStr

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.approvals import builtin
from domainkit.contrib.approvals.settings import ApprovalsSettings
from domainkit.contrib.keys.operations import (
    RINGS_KEPT,
    SettingsKeyring,
    _fingerprint,
)
from domainkit.contrib.keys.settings import KeysSettings
from domainkit.keys.ring import keyring
from domainkit.money import Money
from domainkit.testing import IN_MEMORY
from domainkit_tests.conftest import ALPHA, CONTENTTYPES


@pytest.fixture
def keys() -> Iterator[None]:
    """The keys app, set up."""
    registry.clear()
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        INSTALLED_DOMAINS=[CONTENTTYPES, "domainkit.contrib.keys", ALPHA],
    ):
        setup()
        yield
    registry.clear()


def test_the_ring_is_kept_while_the_settings_and_found_again_after(
    keys: None,
) -> None:
    """One ring per ``KEYS`` value: an unrelated change, or a revert, reuses it."""
    ring = keyring()
    assert keyring() is ring
    with override_settings(DEBUG=True):  # Another setting: the same KEYS.
        assert keyring() is ring
    with override_settings(SECRET_KEY="another-secret-key-of-enough-length-0123"):
        rotated = keyring()
        assert rotated is not ring  # ``dev`` derives from SECRET_KEY.
    assert keyring() is ring


def test_rings_kept_are_bounded(keys: None) -> None:
    """Past ``RINGS_KEPT`` values, the oldest ring is let go."""
    rings = SettingsKeyring()
    first = rings()
    for number in range(RINGS_KEPT):
        with override_settings(SECRET_KEY=f"secret-key-number-{number:04d}-{'x' * 32}"):
            rings()
    assert len(rings._rings) == RINGS_KEPT
    assert rings() is not first


def test_a_secret_setting_tells_rings_apart() -> None:
    """Two Vault tokens are two rings, though the settings print alike."""
    vault: dict[str, Any] = {
        "URL": "https://vault.example",
        "KEY": "data",
        "TOKEN": SecretStr("one"),
    }
    data_keys = {"DATA_KEYS": {"k": "vault:v1:abc"}, "PROVIDER": "vault"}
    one = KeysSettings.model_validate({**data_keys, "VAULT": vault})
    two = KeysSettings.model_validate(
        {**data_keys, "VAULT": {**vault, "TOKEN": SecretStr("two")}}
    )

    with override_settings(SECRET_KEY="a-secret-key-for-fingerprints-0123456789"):
        assert _fingerprint(one) != _fingerprint(two)
        assert _fingerprint(one) == _fingerprint(one.model_copy())


def test_the_entry_threshold_is_read_from_the_settings_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed ``ENTRY_THRESHOLDS`` holds the next entry; unchanged, it is kept."""
    current = ApprovalsSettings.model_validate({"ENTRY_THRESHOLDS": {"USD": "100"}})
    monkeypatch.setattr(registry, "settings", lambda kind: current)
    monkeypatch.setattr(builtin, "_threshold", None)
    big = [Money.of("50", "USD")]

    first = builtin.current_threshold()
    assert not first.exceeded_by(big)
    assert builtin.current_threshold() is first

    current = ApprovalsSettings.model_validate({"ENTRY_THRESHOLDS": {"USD": "10"}})
    assert builtin.current_threshold().exceeded_by(big)
