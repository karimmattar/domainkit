"""Install ``domainkit.contrib.ciba`` to approve sign-ins from the user's phone."""

from collections.abc import Mapping
from importlib import import_module

from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_erase_subject, on_export_subject
from domainkit.db.routing import router
from domainkit.ddd import event_bus
from domainkit.exceptions import ImproperlyConfigured

from .settings import CibaSettings


class CibaConfig(DomainConfig):
    """Sign-ins approved from a bound phone: CIBA clients, and the project's web."""

    label = "ciba"
    verbose_name = "Sign-in approvals"
    settings_class = CibaSettings
    requires = (
        "domainkit.contrib.auth",
        "domainkit.contrib.messaging",
        "domainkit.contrib.notifications",
        "domainkit.contrib.devices",
    )

    def ready(self) -> None:
        """Check the tables share auth's database; gate logins; forget the deleted.

        Raises:
            ImproperlyConfigured: ``ciba`` is routed elsewhere than ``auth``.
        """
        if router.target_for(self.label) != router.target_for("auth"):
            raise ImproperlyConfigured(
                "DATABASE_ROUTING: 'ciba' must be routed with 'auth': an approval "
                "reads the user's devices and records its event on the user, in "
                "one transaction."
            )
        from domainkit.contrib.auth.integration import UserDeleted, on_login_gate

        from .gate import approved_from_the_phone

        on_login_gate(approved_from_the_phone)
        event_bus.subscribe(UserDeleted, forget_requests)
        on_erase_subject(self.erase_person, domain=self.label)
        on_export_subject(self.export_person, domain=self.label)
        import_module(f"{self.name}.tasks")

    async def erase_person(self, subject: str, reason: str) -> Mapping[str, int]:
        """Remove what this app keeps of ``subject`` (``privacy erase``)."""
        from .personal import erase_subject

        return await erase_subject(subject)

    async def export_person(self, subject: str) -> Mapping[str, object]:
        """What this app keeps of ``subject`` (``privacy export``)."""
        from .personal import export_subject

        return await export_subject(subject)


async def forget_requests(event: object) -> None:
    """Remove every sign-in request of a deleted user."""
    from .stores import CibaUnitOfWork

    async with CibaUnitOfWork() as uow:
        await uow.requests.forget_user(event.user_id)  # type: ignore[attr-defined]
        await uow.commit()
