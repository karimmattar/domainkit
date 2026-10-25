"""Install ``domainkit.contrib.notifications``, with messaging."""

from domainkit.apps import DomainConfig, registry
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.contrib.messaging import task
from domainkit.exceptions import ImproperlyConfigured

from .delivery import (
    PUSH_TASK,
    SMS_TASK,
    get_push_backend,
    get_sms_backend,
    send_queued_push,
    send_queued_sms,
)
from .settings import NotificationsSettings

MESSAGING = "domainkit.contrib.messaging"


class NotificationsConfig(DomainConfig):
    """Text messages and push notifications through the outbox."""

    label = "notifications"
    verbose_name = "Notifications"
    settings_class = NotificationsSettings

    def ready(self) -> None:
        """Register the sending tasks, and check both backends now.

        Raises:
            ImproperlyConfigured: Messaging is missing, a backend is
                unusable, or one is made for development where deployed.
        """
        if not registry.is_installed(MESSAGING):
            raise ImproperlyConfigured(
                f"domainkit.contrib.notifications needs {MESSAGING!r} in "
                "INSTALLED_DOMAINS: the worker sends the messages."
            )
        config = registry.settings(NotificationsSettings)
        for name, function in (
            (SMS_TASK, send_queued_sms),
            (PUSH_TASK, send_queued_push),
        ):
            task(  # Numbers, tokens, codes: blanked once dead too.
                name=name,
                max_attempts=config.MAX_ATTEMPTS,
                queue=config.QUEUE,
                sensitive=True,
            )(function)
        backends = {
            "SMS_BACKEND": get_sms_backend().deployable,
            "PUSH_BACKEND": get_push_backend().deployable,
        }
        if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
            for setting, deployable in backends.items():
                if not deployable:
                    raise ImproperlyConfigured(
                        f"NOTIFICATIONS.{setting} {getattr(config, setting)!r} is "
                        f"for development and tests; write an adapter for your "
                        f"provider for {settings.ENVIRONMENT}."
                    )
