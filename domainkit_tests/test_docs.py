"""The reference documentation names every setting and command."""

import re
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import BaseModel

from domainkit.conf.global_settings import (
    BrokerConfig,
    CacheConfig,
    DatabaseConfig,
    DebugToolbarConfig,
    HttpClientConfig,
    LedgerConfig,
    Settings,
    ShardConfig,
    StorageConfig,
)
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.contrib.auth.settings import (
    AuthSettings,
    ClientCertSettings,
    CookieSettings,
    JWTSettings,
    MfaSettings,
)
from domainkit.contrib.evidence.settings import EvidenceSettings
from domainkit.contrib.flags.settings import FlagsSettings
from domainkit.contrib.idempotency.settings import IdempotencySettings
from domainkit.contrib.mail.settings import MailSettings, SmtpSettings
from domainkit.contrib.messaging.settings import MessagingSettings
from domainkit.contrib.notifications.settings import NotificationsSettings
from domainkit.contrib.webhooks.settings import WebhookSettings
from domainkit.management import BUILTIN_COMMANDS, _find_commands

DOCS = Path(__file__).parents[1] / "docs"
CONTRIB_COMMANDS = (
    "domainkit.contrib.messaging.management.commands",
    "domainkit.contrib.audit.management.commands",
    "domainkit.contrib.evidence.management.commands",
    "domainkit.contrib.auth.management.commands",
    "domainkit.contrib.flags.management.commands",
    "domainkit.contrib.webhooks.management.commands",
    "domainkit.contrib.privacy.management.commands",
)


def documented(page: str) -> set[str]:
    """Return every word written in backticks on ``page``."""
    text = re.sub(r"```.*?```", "", (DOCS / page).read_text(), flags=re.DOTALL)
    return {word for code in re.findall(r"`([^`]+)`", text) for word in code.split()}


@pytest.mark.parametrize(
    ("model", "page"),
    [
        (Settings, "reference.md"),
        (DatabaseConfig, "reference.md"),
        (LedgerConfig, "reference.md"),
        (ShardConfig, "reference.md"),
        (CacheConfig, "reference.md"),
        (BrokerConfig, "reference.md"),
        (StorageConfig, "reference.md"),
        (DebugToolbarConfig, "reference.md"),
        (HttpClientConfig, "reference.md"),
        (MessagingSettings, "contrib.md"),
        (AuditSettings, "contrib.md"),
        (IdempotencySettings, "contrib.md"),
        (EvidenceSettings, "contrib.md"),
        (FlagsSettings, "contrib.md"),
        (WebhookSettings, "contrib.md"),
        (AuthSettings, "auth.md"),
        (JWTSettings, "auth.md"),
        (CookieSettings, "auth.md"),
        (MfaSettings, "auth.md"),
        (ClientCertSettings, "auth.md"),
        (MailSettings, "mail.md"),
        (SmtpSettings, "mail.md"),
        (NotificationsSettings, "mail.md"),
    ],
)
def test_every_setting_is_documented(model: type[BaseModel], page: str) -> None:
    """A new field needs a row in its table."""
    assert set(model.model_fields) - documented(page) == set()


def test_every_command_is_documented() -> None:
    """Built-in and contrib commands each have a row."""
    commands = set(_find_commands(import_module(BUILTIN_COMMANDS).__path__))
    for package in CONTRIB_COMMANDS:
        commands |= set(_find_commands(import_module(package).__path__))

    assert commands - documented("reference.md") == set()
