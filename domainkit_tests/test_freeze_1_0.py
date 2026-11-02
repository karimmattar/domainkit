"""The 1.0 freeze (ADR 0125), and what came after it.

The names 0.12 had in two homes live in one now (DSGN-29): the old homes
and spellings are gone. The defaults 1.0 flipped are what a settings module
that says nothing gets.
"""

import importlib
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest
from pydantic import ValidationError

from domainkit.apps import DomainConfig
from domainkit.conf import UNSET as SETTINGS_UNSET
from domainkit.conf import Settings
from domainkit.contrib.auth import AnonymousUser
from domainkit.contrib.auth.authentication import Credentials
from domainkit.contrib.messaging import MessagingSettings
from domainkit.contrib.privacy import Retention
from domainkit.contrib.webhooks import WebhooksSettings
from domainkit.ddd import UNSET
from domainkit.deprecation import DomainkitDeprecationWarning, renamed
from domainkit.resources import UNSET as RESOURCES_UNSET

ROOT: Final = Path(__file__).parents[1]
KEPT: Final = Retention(table="old_log", field="at", keep=timedelta(days=30))


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("domainkit.apps.hooks", "on_check"),
        ("domainkit.contrib.webhooks", "WebhookSettings"),
        ("domainkit.contrib.webhooks.settings", "WebhookSettings"),
        ("domainkit.contrib.contenttypes", "InstalledModel"),
        ("domainkit.contrib.contenttypes", "content_type_key"),
        ("domainkit.contrib.contenttypes", "installed_models"),
    ],
)
def test_names_with_two_homes_have_one(module: str, name: str) -> None:
    """The names 0.12 had elsewhere live only in their one home now (DSGN-29)."""
    with pytest.raises(AttributeError, match=f"has no attribute '{name}'"):
        getattr(importlib.import_module(module), name)


def test_old_homes_and_spellings_are_gone() -> None:
    """The moved module, the old setting, property, and class attribute."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("domainkit.contrib.messaging.dbclock")
    with pytest.raises(ValidationError, match="SHUTDOWN_TIMEOUT"):
        MessagingSettings(SHUTDOWN_TIMEOUT=5)  # type: ignore[call-arg]
    credentials = Credentials(
        AnonymousUser(),  # type: ignore[arg-type]
        scheme="jwt",
        login=uuid4(),
    )
    assert not hasattr(credentials, "session")

    class OldConfig(DomainConfig):
        RETENTION = (KEPT,)

    assert OldConfig.retention == ()
    assert WebhooksSettings is not None


def test_renamed_names_its_new_place() -> None:
    """The deprecation helper, for the renames to come, says what to write."""
    answer = renamed("pkg.old", {"a": "domainkit.ddd.UNSET"}, "2.0")
    with pytest.warns(
        DomainkitDeprecationWarning, match=r"^pkg\.old\.a is now domainkit\.ddd\.UNSET"
    ):
        assert answer("a") is UNSET


def test_one_unset_sentinel() -> None:
    """``override_settings(X=UNSET)`` and a partial command use one object."""
    assert SETTINGS_UNSET is UNSET is RESOURCES_UNSET
    assert repr(UNSET) == "UNSET"


def test_the_defaults_1_0_flipped() -> None:
    """Route names as operation ids, and problem details as error bodies."""
    defaults = Settings()
    assert defaults.OPENAPI_OPERATION_IDS == "route_name"
    assert defaults.ERROR_FORMAT == "problem+json"


def test_the_keys_extra_installs_what_encrypted_fields_need() -> None:
    """AES-GCM, and the HTTP client the key services are called through."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    keys = project["project"]["optional-dependencies"]["keys"]
    assert {spec.split(" ")[0] for spec in keys} == {
        "cryptography",
        "httpx2",
        "httpcore2",
    }
