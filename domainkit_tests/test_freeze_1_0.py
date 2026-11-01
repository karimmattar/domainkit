"""The 1.0 freeze (ADR 0125): renamed names of 0.12 still answer, with a warning.

Each old name of a published release works through 1.x and warns with
``DomainkitDeprecationWarning``, naming the new one and 2.0; the defaults
1.0 flipped are what a settings module that says nothing gets.
"""

import importlib
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest

from domainkit.apps import DomainConfig
from domainkit.apps import hooks as hooks_module
from domainkit.conf import UNSET as SETTINGS_UNSET
from domainkit.conf import Settings
from domainkit.contrib.auth import AnonymousUser
from domainkit.contrib.auth.authentication import Credentials
from domainkit.contrib.messaging import MessagingSettings
from domainkit.contrib.privacy import Retention
from domainkit.contrib.webhooks import WebhooksSettings
from domainkit.db import dbclock, models
from domainkit.ddd import UNSET
from domainkit.deprecation import DomainkitDeprecationWarning, renamed
from domainkit.resources import UNSET as RESOURCES_UNSET

ROOT: Final = Path(__file__).parents[1]
KEPT: Final = Retention(table="old_log", field="at", keep=timedelta(days=30))


def deprecated(module: str, name: str) -> Any:  # noqa: ANN401 - whatever it names.
    """``module.name``, asserting that reading it warns about 2.0."""
    with pytest.warns(DomainkitDeprecationWarning, match=r"removed in .*2\.0"):
        return getattr(importlib.import_module(module), name)


@pytest.mark.parametrize(
    ("module", "name", "new"),
    [
        ("domainkit.apps.hooks", "on_check", hooks_module.on_ready),
        ("domainkit.contrib.webhooks", "WebhookSettings", WebhooksSettings),
        ("domainkit.contrib.webhooks.settings", "WebhookSettings", WebhooksSettings),
        ("domainkit.contrib.messaging.dbclock", "now", dbclock.now),
        ("domainkit.contrib.messaging.dbclock", "Moment", dbclock.Moment),
        ("domainkit.contrib.contenttypes", "InstalledModel", models.InstalledModel),
        ("domainkit.contrib.contenttypes", "content_type_key", models.model_key),
        ("domainkit.contrib.contenttypes", "installed_models", models.installed_models),
    ],
)
def test_a_renamed_name_still_answers_with_a_warning(
    module: str, name: str, new: object
) -> None:
    """The old name is the new name's object, so ``isinstance`` still holds."""
    assert deprecated(module, name) is new


@pytest.mark.parametrize(
    "module",
    [
        "domainkit.apps.hooks",
        "domainkit.contrib.contenttypes",
        "domainkit.contrib.messaging.dbclock",
    ],
)
def test_other_missing_names_still_raise(module: str) -> None:
    """Only the renamed names are answered; a typo is an ``AttributeError``."""
    with pytest.raises(AttributeError, match="has no attribute 'nothing'"):
        importlib.import_module(module).nothing  # noqa: B018 - the access raises.


def test_renamed_names_its_new_place() -> None:
    """The warning says what to write instead."""
    answer = renamed("pkg.old", {"a": "domainkit.ddd.UNSET"}, "2.0")
    with pytest.warns(
        DomainkitDeprecationWarning, match=r"^pkg\.old\.a is now domainkit\.ddd\.UNSET"
    ):
        assert answer("a") is UNSET


def test_a_domain_declaring_retention_the_old_way_is_read_with_a_warning() -> None:
    """``RETENTION`` on a subclass becomes its ``retention``."""
    with pytest.warns(DomainkitDeprecationWarning, match=r"\.RETENTION is now"):

        class OldConfig(DomainConfig):
            RETENTION = (KEPT,)

    assert OldConfig.retention == (KEPT,)

    class NewConfig(DomainConfig):
        retention = (KEPT,)

    assert NewConfig.retention == (KEPT,)


def test_messaging_reads_shutdown_timeout_as_drain_timeout() -> None:
    """0.12's key still sets the drain; a new key given too wins."""
    with pytest.warns(DomainkitDeprecationWarning, match="DRAIN_TIMEOUT"):
        old = MessagingSettings(SHUTDOWN_TIMEOUT=5)  # type: ignore[call-arg]
    assert old.DRAIN_TIMEOUT == 5
    with pytest.warns(DomainkitDeprecationWarning):
        both = MessagingSettings(SHUTDOWN_TIMEOUT=5, DRAIN_TIMEOUT=7)  # type: ignore[call-arg]
    assert both.DRAIN_TIMEOUT == 7
    assert MessagingSettings().DRAIN_TIMEOUT == 20


def test_credentials_session_reads_login_with_a_warning() -> None:
    """The family of a JWT login is ``login``; ``session`` reads it."""
    family = uuid4()
    credentials = Credentials(
        AnonymousUser(),  # type: ignore[arg-type]
        scheme="jwt",
        login=family,
    )
    with pytest.warns(DomainkitDeprecationWarning, match="Credentials.login"):
        assert credentials.session == family


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
