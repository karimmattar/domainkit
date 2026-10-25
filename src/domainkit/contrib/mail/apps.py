"""Install ``domainkit.contrib.mail`` (with ``domainkit.contrib.messaging``)."""

from domainkit.apps import DomainConfig, registry
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.contrib.messaging import task
from domainkit.exceptions import ImproperlyConfigured

from .delivery import SEND_TASK, get_backend, send_queued
from .settings import MailSettings

MESSAGING = "domainkit.contrib.messaging"


class MailConfig(DomainConfig):
    """Email through the outbox: queued with the commit, sent by the worker."""

    label = "mail"
    verbose_name = "Mail"
    settings_class = MailSettings

    def ready(self) -> None:
        """Register the sending task, and check the backend now, not at first use.

        Raises:
            ImproperlyConfigured: Messaging is missing, the backend is
                unusable, or it is made for development where deployed.
        """
        if not registry.is_installed(MESSAGING):
            raise ImproperlyConfigured(
                f"domainkit.contrib.mail needs {MESSAGING!r} in INSTALLED_DOMAINS: "
                "the worker sends the mail."
            )
        config = registry.settings(MailSettings)
        # Mail holds addresses, and often links and codes: blanked once dead too.
        task(
            name=SEND_TASK,
            max_attempts=config.MAX_ATTEMPTS,
            queue=config.QUEUE,
            sensitive=True,
        )(send_queued)
        backend = get_backend()
        if not backend.deployable and settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
            raise ImproperlyConfigured(
                f"MAIL.BACKEND {config.BACKEND!r} is for development and tests; "
                f"use one that delivers mail in {settings.ENVIRONMENT}."
            )
