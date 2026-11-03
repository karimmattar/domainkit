"""Install ``domainkit.contrib.phones`` to verify users' phone numbers."""

from domainkit.apps import DomainConfig
from domainkit.db.routing import router
from domainkit.ddd import event_bus
from domainkit.exceptions import ImproperlyConfigured

from .settings import PhonesSettings


class PhonesConfig(DomainConfig):
    """Codes sent by text message, and the numbers users proved they receive."""

    label = "phones"
    verbose_name = "Phone numbers"
    settings_class = PhonesSettings
    requires = (
        "domainkit.contrib.auth",
        "domainkit.contrib.messaging",
        "domainkit.contrib.notifications",
    )

    def ready(self) -> None:
        """Check the phone tables share auth's database; forget deleted users.

        Raises:
            ImproperlyConfigured: ``phones`` is routed elsewhere than ``auth``.
        """
        if router.target_for(self.label) != router.target_for("auth"):
            raise ImproperlyConfigured(
                "DATABASE_ROUTING: 'phones' must be routed with 'auth': a "
                "verification records its event on the user, in one transaction."
            )
        from domainkit.contrib.auth.integration import UserDeleted

        event_bus.subscribe(UserDeleted, forget_phones)


async def forget_phones(event: object) -> None:
    """Remove every number and code of a deleted user."""
    from .stores import PhonesUnitOfWork

    user_id = event.user_id  # type: ignore[attr-defined]
    async with PhonesUnitOfWork() as uow:
        await uow.phones.forget_user(user_id)
        await uow.codes.forget_user(user_id)
        await uow.commit()
