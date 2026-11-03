"""Install ``domainkit.contrib.devices`` for passkeys and bound devices."""

from domainkit.apps import DomainConfig
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
            ImproperlyConfigured: ``devices`` is routed elsewhere than ``auth``.
        """
        if router.target_for(self.label) != router.target_for("auth"):
            raise ImproperlyConfigured(
                "DATABASE_ROUTING: 'devices' must be routed with 'auth': a login "
                "reads both in one transaction."
            )
        from domainkit.apps import registry
        from domainkit.contrib.auth.events import UserDeleted

        event_bus.subscribe(UserDeleted, forget_devices)
        if registry.is_installed("domainkit.contrib.notifications"):
            from domainkit.contrib.notifications.events import PushTokenExpired

            event_bus.subscribe(PushTokenExpired, forget_push_token)


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
