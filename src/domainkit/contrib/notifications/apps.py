"""Install ``domainkit.contrib.notifications``, with messaging."""

from domainkit.apps import DomainConfig, Requirement, registry
from domainkit.checks import CheckFailed, CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.contrib.messaging import task

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
    requires = (Requirement(MESSAGING, "the worker sends the messages"),)

    def ready(self) -> None:
        """Register the sending tasks, and check both backends now.

        Raises:
            ImproperlyConfigured: A backend is unusable, or one is made for
                development where deployed.
        """
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
            "SMS_BACKEND": (config.SMS_BACKEND, get_sms_backend().deployable),
            "PUSH_BACKEND": (config.PUSH_BACKEND, get_push_backend().deployable),
        }
        for platform, path in config.PUSH_BACKENDS.items():
            backends[f"PUSH_BACKENDS[{platform!r}]"] = (
                path,
                get_push_backend(platform).deployable,
            )
        if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS and (
            refused := [
                CheckMessage(
                    "deploy.E005" if setting == "SMS_BACKEND" else "deploy.E006",
                    f"NOTIFICATIONS.{setting} {path!r} is for development and tests",
                    f"Write an adapter for your provider for {settings.ENVIRONMENT}.",
                )
                for setting, (path, deployable) in backends.items()
                if not deployable
            ]
        ):
            raise CheckFailed("NOTIFICATIONS settings cannot be deployed", refused)
