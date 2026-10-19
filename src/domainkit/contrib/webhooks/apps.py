"""Install ``domainkit.contrib.webhooks`` to send events to subscribed URLs."""

import ipaddress
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

    def ready(self) -> None:
        """Check the requirements and settings; map the tables; register tasks.

        Webhooks need messaging and one database. Staging and production
        refuse loopback and link-local networks among
        ``ALLOWED_PRIVATE_NETWORKS``.
        """
        if not registry.is_installed(MESSAGING):
            raise ImproperlyConfigured(
                f"domainkit.contrib.webhooks needs {MESSAGING!r} in INSTALLED_DOMAINS."
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
