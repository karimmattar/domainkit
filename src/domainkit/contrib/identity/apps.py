"""Install ``domainkit.contrib.identity`` to log in through identity providers."""

from domainkit.apps import DomainConfig
from domainkit.db.routing import router
from domainkit.ddd import event_bus
from domainkit.exceptions import ImproperlyConfigured

from .settings import IdentitySettings


class IdentityConfig(DomainConfig):
    """OpenID Connect, OAuth 2, SAML, and LDAP logins, linked to auth's users."""

    label = "identity"
    verbose_name = "Identity providers"
    settings_class = IdentitySettings
    requires = ("domainkit.contrib.auth",)

    def ready(self) -> None:
        """Check the identity tables share auth's database; forget deleted users.

        Raises:
            ImproperlyConfigured: ``identity`` is routed elsewhere than ``auth``.
        """
        if router.target_for(self.label) != router.target_for("auth"):
            raise ImproperlyConfigured(
                "DATABASE_ROUTING: 'identity' must be routed with 'auth': a login "
                "reads both in one transaction."
            )
        from domainkit.contrib.auth.integration import UserDeleted

        event_bus.subscribe(UserDeleted, forget_identities)


async def forget_identities(event: object) -> None:
    """Unlink every identity of a deleted user."""
    from .stores import IdentityUnitOfWork

    async with IdentityUnitOfWork() as uow:
        await uow.identities.forget_user(event.user_id)  # type: ignore[attr-defined]
        await uow.commit()
