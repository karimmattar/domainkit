"""Install ``domainkit.contrib.devices`` for passkeys and bound devices."""

from collections.abc import Mapping

from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_erase_subject, on_export_subject
from domainkit.db.routing import router
from domainkit.ddd import event_bus
from domainkit.exceptions import ImproperlyConfigured

from .settings import DevicesSettings


class DevicesConfig(DomainConfig):
    """Passkeys (WebAuthn) and device-bound keys of auth's users."""

    label = "devices"
    verbose_name = "Passkeys and devices"
    settings_class = DevicesSettings
    requires = ("domainkit.contrib.auth",)

    def ready(self) -> None:
        """Check the device tables share auth's database; forget deleted users.

        Raises:
            ImproperlyConfigured: ``devices`` is routed elsewhere than ``auth``,
                ``ENROLLMENT`` is on without ``domainkit.contrib.messaging``,
                or ``SIGNATURE_STORE`` is a cache that remembers nothing, or
                that each process keeps alone where deployed.
        """
        if router.target_for(self.label) != router.target_for("auth"):
            raise ImproperlyConfigured(
                "DATABASE_ROUTING: 'devices' must be routed with 'auth': a login "
                "reads both in one transaction."
            )
        from domainkit.apps import registry
        from domainkit.contrib.auth.integration import UserDeleted

        messaging = registry.is_installed("domainkit.contrib.messaging")
        if registry.settings(DevicesSettings).ENROLLMENT and not messaging:
            raise ImproperlyConfigured(
                "DEVICES.ENROLLMENT needs 'domainkit.contrib.messaging' in "
                "INSTALLED_DOMAINS: its worker sends the codes."
            )

        _check_signature_store(registry.settings(DevicesSettings).SIGNATURE_STORE)

        event_bus.subscribe(UserDeleted, forget_devices)
        on_erase_subject(self.erase_person, domain=self.label)
        on_export_subject(self.export_person, domain=self.label)
        if messaging:
            from .enrollment import register_tasks
            from .tasks import register_notice, register_purge

            register_tasks()
            register_purge()
            if registry.is_installed("domainkit.contrib.mail"):
                register_notice()
        if registry.is_installed("domainkit.contrib.notifications"):
            from domainkit.contrib.notifications.events import PushTokenExpired

            event_bus.subscribe(PushTokenExpired, forget_push_token)

    async def erase_person(self, subject: str, reason: str) -> Mapping[str, int]:
        """Remove what this app keeps of ``subject`` (``privacy erase``)."""
        from .personal import erase_subject

        return await erase_subject(subject)

    async def export_person(self, subject: str) -> Mapping[str, object]:
        """What this app keeps of ``subject`` (``privacy export``)."""
        from .personal import export_subject

        return await export_subject(subject)


def _check_signature_store(store: str) -> None:
    """Refuse a cache that could let a signed request work twice.

    Raises:
        ImproperlyConfigured: ``SIGNATURE_STORE`` is ``"cache"``, and the
            default cache keeps nothing (``dummy://``), or is a process's
            own memory in staging or production: another process would
            accept the same request again.
    """
    if store != "cache":
        return
    from domainkit.cache import caches
    from domainkit.conf import settings
    from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS, REDIS_SCHEMES

    config = caches.config()
    if config.BACKEND is not None or config.scheme in REDIS_SCHEMES:
        return
    if config.scheme == "dummy" or settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
        raise ImproperlyConfigured(
            'DEVICES.SIGNATURE_STORE is "cache", but CACHES["default"] '
            f"({config.scheme}://) is not shared by every process: a signed "
            "request could work twice. Use a Redis cache, or "
            'SIGNATURE_STORE = "database".'
        )


async def forget_devices(event: object) -> None:
    """Remove every passkey, device, and push token of a deleted user."""
    from .stores import DevicesUnitOfWork

    user_id = event.user_id  # type: ignore[attr-defined]
    async with DevicesUnitOfWork() as uow:
        await uow.passkeys.forget_user(user_id)
        await uow.devices.forget_user(user_id)
        await uow.push_tokens.forget_user(user_id)
        await uow.commit()


async def forget_push_token(event: object) -> None:
    """Remove a token its push service no longer knows."""
    from .stores import DevicesUnitOfWork

    async with DevicesUnitOfWork() as uow:
        await uow.push_tokens.forget(event.token_digest)  # type: ignore[attr-defined]
        await uow.commit()
