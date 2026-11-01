"""Install ``domainkit.contrib.keys`` for encrypted fields and crypto-shredding."""

from collections.abc import Mapping

from fastapi import FastAPI

from domainkit.apps import DomainConfig, registry
from domainkit.apps.hooks import (
    on_check_application,
    on_deployment_warnings,
    on_erase_subject,
    on_startup,
)
from domainkit.checks import CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db.routing import DatabaseRouter
from domainkit.keys.ring import keyring, use_keyring

from .operations import ring_for, shred_subject
from .settings import KeysSettings


class KeysConfig(DomainConfig):
    """Seal encrypted fields under ``KEYS``; destroy a person's keys on erasure."""

    label = "keys"
    verbose_name = "Keys"
    settings_class = KeysSettings
    requires = ()

    def ready(self) -> None:
        """Set up the process's key ring from ``KEYS``, unwrapped at startup.

        Erasing a person (``erase_subject``, ``domainkit.contrib.privacy``)
        destroys their keys.
        """
        use_keyring(ring_for(registry.settings(KeysSettings)))
        on_startup(self.start)
        on_check_application(self.application_problems)
        on_deployment_warnings(self.retired_keys)
        on_erase_subject(self.shred)

    async def start(self) -> None:
        """Unwrap the data keys now, so a key service that refuses stops startup."""
        await keyring().load()

    def application_problems(self, app: FastAPI) -> list[str]:
        """Key services anyone could read through, where deployed.

        ``keys.E001``: the local KEK, unless allowed. Its key derives from
        ``SECRET_KEY``, which the application holds, so whoever reads the
        settings unwraps every data key: no key management service stands
        between them. ``keys.E002``: Vault over plain HTTP, which sends the
        token, and every data key it unwraps, in clear (SEC-15a-6).
        """
        if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
            return []
        config = registry.settings(KeysSettings)
        problems: list[str] = []
        if config.PROVIDER == "local" and not config.ALLOW_LOCAL_KEK:
            problems.append(
                CheckMessage(
                    "keys.E001",
                    f"KEYS.PROVIDER is 'local' in {settings.ENVIRONMENT}: the key "
                    "that wraps the data keys derives from SECRET_KEY, so anyone "
                    "who reads the settings can open every encrypted field.",
                    hint='Set KEYS.PROVIDER to "aws-kms" or "vault", or '
                    "KEYS.ALLOW_LOCAL_KEK = True to accept it.",
                )
            )
        vault = config.VAULT
        if (
            config.PROVIDER == "vault"
            and vault is not None
            and not vault.URL.startswith("https://")
        ):
            problems.append(
                CheckMessage(
                    "keys.E002",
                    f"KEYS.VAULT.URL is plain HTTP in {settings.ENVIRONMENT}: "
                    "Vault's token, and every data key it unwraps, would cross "
                    "the network in clear.",
                    hint="Serve Vault over TLS and give an https:// URL.",
                )
            )
        return problems

    def retired_keys(self) -> list[str]:
        """Data keys kept only to open what they sealed (``keys.W001``).

        Values sealed under a retired key read as long as it stays in
        ``DATA_KEYS``; ``manage.py keys status`` counts them, and
        ``manage.py keys reencrypt`` moves them to the current key.
        """
        retired = registry.settings(KeysSettings).retired
        if not retired:
            return []
        return [
            CheckMessage(
                "keys.W001",
                f"KEYS.DATA_KEYS keeps retired keys ({', '.join(retired)}): values "
                "and subjects' keys may still be sealed under them.",
                hint="Run 'manage.py keys reencrypt', then 'manage.py keys status "
                "--check'; remove the keys once nothing uses them (for dev, set "
                "KEYS.KEEP_DEVELOPMENT_KEY = False).",
            )
        ]

    async def shred(self, subject: str, reason: str) -> Mapping[str, int]:
        """Destroy the person's keys: what they sealed reads as None from now on."""
        return {"subject_keys": await shred_subject(subject)}

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Every hosting target: subjects' keys live beside what they seal."""
        return frozenset(router.hosting_targets())
