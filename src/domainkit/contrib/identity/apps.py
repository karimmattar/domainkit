"""Install ``domainkit.contrib.identity`` to log in through identity providers."""

from domainkit.apps import DomainConfig, registry
from domainkit.checks import CheckFailed, CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
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

        Staging and production also refuse a directory reached without TLS
        (``security.E020``): every password a login checks would cross the
        network in the clear.

        Raises:
            ImproperlyConfigured: ``identity`` is routed elsewhere than ``auth``.
            CheckFailed: An ``ldap://`` directory without ``START_TLS``,
                deployed.
        """
        if router.target_for(self.label) != router.target_for("auth"):
            raise ImproperlyConfigured(
                "DATABASE_ROUTING: 'identity' must be routed with 'auth': a login "
                "reads both in one transaction."
            )
        ldap = registry.settings(IdentitySettings).LDAP
        if (
            ldap is not None
            and settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS
            and not ldap.URL.lower().startswith("ldaps://")
            and not ldap.START_TLS
        ):
            raise CheckFailed(
                "IDENTITY settings cannot be deployed",
                [
                    CheckMessage(
                        "security.E020",
                        f"IDENTITY.LDAP reaches the directory without TLS in "
                        f"{settings.ENVIRONMENT}",
                        "Use an ldaps:// URL, or set START_TLS: every password "
                        "a login checks crosses the network.",
                    )
                ],
            )
        from domainkit.contrib.auth.integration import UserDeleted

        event_bus.subscribe(UserDeleted, forget_identities)


async def forget_identities(event: object) -> None:
    """Unlink every identity of a deleted user."""
    from .stores import IdentityUnitOfWork

    async with IdentityUnitOfWork() as uow:
        await uow.identities.forget_user(event.user_id)  # type: ignore[attr-defined]
        await uow.commit()
