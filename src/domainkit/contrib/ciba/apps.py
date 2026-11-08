"""Install ``domainkit.contrib.ciba`` to approve sign-ins from the user's phone."""

from collections.abc import Mapping
from importlib import import_module
from typing import TYPE_CHECKING

from domainkit.apps import DomainConfig
from domainkit.apps.hooks import (
    on_check_application,
    on_erase_subject,
    on_export_subject,
)
from domainkit.checks import CheckMessage
from domainkit.db.routing import router
from domainkit.ddd import event_bus
from domainkit.exceptions import ImproperlyConfigured

from .settings import CibaSettings

if TYPE_CHECKING:
    from fastapi import APIRouter, FastAPI


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
        on_check_application(self.application_problems)
        import_module(f"{self.name}.tasks")

    def application_problems(self, app: "FastAPI") -> list[str]:
        """What the routes served need and lack.

        ``ciba.E002``: the OpenID Provider's routes are served, but
        ``AUTH.JWT`` cannot sign ID tokens others verify: no ``ISSUER``, or
        a shared-secret algorithm. ``ciba.E003``: ``APPROVE_LOGINS``
        refuses web logins of users with a bound device, but the routes
        that let them sign in from the web, or answer from the phone, are
        not served. ``ciba.E004``: ``/app-sign-in/session`` is served, but
        no authentication class reads the browser session it starts.
        ``ciba.E005``: ``APPROVE_LOGINS`` is ``"always"``, but devices do not
        enroll (``DEVICES.ENROLLMENT``): nobody could log in.
        """
        from domainkit.apps import registry
        from domainkit.contrib.auth.integration import auth_settings
        from domainkit.contrib.devices.api import router as devices_router
        from domainkit.contrib.devices.settings import DevicesSettings

        from .api import router as sign_in_router
        from .api import start_approved_session
        from .oidc import router as oidc_router

        problems: list[str] = []
        jwt = auth_settings().JWT
        if _serves(app, oidc_router) and (
            jwt.ISSUER is None or jwt.ALGORITHM.startswith("HS")
        ):
            problems.append(
                CheckMessage(
                    "ciba.E002",
                    "Routes of domainkit.contrib.ciba.oidc issue ID tokens, but "
                    "AUTH.JWT has no ISSUER or signs with a shared secret "
                    f"({jwt.ALGORITHM}): clients could not verify them.",
                    hint="Set AUTH.JWT.ALGORITHM to RS256, ES256 or EdDSA, its "
                    "SIGNING_KEY and VERIFYING_KEY, and an ISSUER.",
                )
            )
        if registry.settings(CibaSettings).APPROVE_LOGINS and not (
            _serves(app, sign_in_router) and _serves(app, devices_router)
        ):
            problems.append(
                CheckMessage(
                    "ciba.E003",
                    "CIBA.APPROVE_LOGINS refuses web logins of users with a bound "
                    "device, but the routes of domainkit.contrib.ciba.api or "
                    "domainkit.contrib.devices.api are not served: those users "
                    "could not sign in on the web, or answer from the phone.",
                    hint="Mount both routers, under /auth for example.",
                )
            )
        if (
            registry.settings(CibaSettings).APPROVE_LOGINS == "always"
            and not registry.settings(DevicesSettings).ENROLLMENT
        ):
            problems.append(
                CheckMessage(
                    "ciba.E005",
                    'CIBA.APPROVE_LOGINS = "always" lets users log in only from '
                    "a bound device, but DEVICES.ENROLLMENT is off: no user "
                    "could bind a first one, nor log in at all.",
                    hint='Set DEVICES = {"ENROLLMENT": True}, or APPROVE_LOGINS '
                    "to True.",
                )
            )
        cookies = "domainkit.contrib.auth.cookies.CookieAuthentication"
        if (
            start_approved_session in _served_endpoints(app)
            and cookies not in auth_settings().AUTHENTICATION_CLASSES
        ):
            problems.append(
                CheckMessage(
                    "ciba.E004",
                    "/app-sign-in/session starts browser sessions, but "
                    f"AUTH.AUTHENTICATION_CLASSES lacks {cookies}: the browser "
                    "would get a cookie nothing reads.",
                    hint=f"Add {cookies} to AUTH.AUTHENTICATION_CLASSES, or "
                    "redeem with /app-sign-in/token.",
                )
            )
        return problems

    async def erase_person(self, subject: str, reason: str) -> Mapping[str, int]:
        """Remove what this app keeps of ``subject`` (``privacy erase``)."""
        from .personal import erase_subject

        return await erase_subject(subject)

    async def export_person(self, subject: str) -> Mapping[str, object]:
        """What this app keeps of ``subject`` (``privacy export``)."""
        from .personal import export_subject

        return await export_subject(subject)


def _served_endpoints(app: "FastAPI") -> set[object]:
    """The functions of the routes ``app`` serves."""
    from domainkit.http.access import served_routes

    return {dependant.call for _, dependant in served_routes(app.routes) if dependant}


def _serves(app: "FastAPI", router: "APIRouter") -> bool:
    """Whether ``app`` serves a route of ``router``."""
    endpoints = {route.endpoint for route in router.routes}  # type: ignore[attr-defined]
    return bool(_served_endpoints(app) & endpoints)


async def forget_requests(event: object) -> None:
    """Remove every sign-in request of a deleted user."""
    from .stores import CibaUnitOfWork

    async with CibaUnitOfWork() as uow:
        await uow.requests.forget_user(event.user_id)  # type: ignore[attr-defined]
        await uow.commit()
