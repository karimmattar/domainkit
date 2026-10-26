"""Install ``domainkit.contrib.webhooks`` to send events to subscribed URLs."""

import importlib.util
import ipaddress
from collections.abc import Mapping
from typing import Final

from domainkit.apps import DomainConfig, registry
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.exceptions import ImproperlyConfigured

from .settings import WebhookSettings

MESSAGING: Final = "domainkit.contrib.messaging"
LOCAL_NETWORKS: Final = tuple(
    ipaddress.ip_network(network)
    for network in ("0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "::/127", "fe80::/10")
)
"""Networks staging and production refuse in ``ALLOWED_PRIVATE_NETWORKS``."""


class WebhooksConfig(DomainConfig):
    """Deliver exposed events to subscriptions, through the messaging outbox."""

    label = "webhooks"
    verbose_name = "Webhooks"
    settings_class = WebhookSettings
    requires = (MESSAGING,)

    def ready(self) -> None:
        """Check the requirements and settings; map the tables; register tasks.

        Webhooks need messaging, one database, ``SECRET_KEY``, and
        cryptography: subscription secrets are encrypted under a key derived
        from ``SECRET_KEY``. Staging and production refuse loopback and
        link-local networks among ``ALLOWED_PRIVATE_NETWORKS``.
        """
        if settings.SECRET_KEY is None:
            raise ImproperlyConfigured(
                "domainkit.contrib.webhooks encrypts subscription secrets under a "
                "key derived from SECRET_KEY: set SECRET_KEY."
            )
        if importlib.util.find_spec("cryptography") is None:
            raise ImproperlyConfigured(
                "domainkit.contrib.webhooks encrypts subscription secrets with "
                'cryptography: pip install "domainkit[webhooks]".'
            )
        config = registry.settings(WebhookSettings)
        if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
            for network in config.ALLOWED_PRIVATE_NETWORKS:
                if any(
                    network.version == local.version and network.overlaps(local)
                    for local in LOCAL_NETWORKS
                ):
                    raise ImproperlyConfigured(
                        f"WEBHOOKS.ALLOWED_PRIVATE_NETWORKS may not reach loopback "
                        f"or link-local addresses in {settings.ENVIRONMENT}: {network}."
                    )
        from .delivery import register, webhooks_alias
        from .tables import start_mappers

        webhooks_alias()  # One database.
        start_mappers()
        register(config)

    async def erase_subject(self, subject: str, reason: str) -> Mapping[str, int]:
        """Blank the deliveries about ``subject``, and cancel the pending ones."""
        from .delivery import erase_subject

        return await erase_subject(subject)

    async def export_subject(self, subject: str) -> Mapping[str, object]:
        """The deliveries about ``subject``, with their bodies."""
        from .delivery import export_subject

        found = await export_subject(subject)
        return {"deliveries": found} if found else {}
