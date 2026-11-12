"""One router for an account's routes: auth's, and those of the apps built on it.

A project mounted each router by hand: logging in, sessions, the second
factor, tokens, recovery, devices, sign-in approvals, eight imports for a
phone-first project. ``account_router`` mounts what is installed::

    from domainkit.contrib.auth.routing import account_router

    router.include_router(account_router(), prefix="/auth")

Each app names its routes (``AccountRoutes``) and says whether they are
mounted by default. Auth's own:

================  =============================  ==========================
Name              Routes                         By default
================  =============================  ==========================
``login``         ``auth.api``                   yes
``sessions``      ``auth.cookies``               with ``CookieAuthentication``
``mfa``           ``auth.mfa``                   yes
``tokens``        ``auth.personal_tokens``       yes
``step-up``       ``auth.stepup``                yes
``sca``           ``auth.sca``                   yes
``recovery``      ``auth.recovery``              with messaging and mail
``signup``        ``auth.signup``                no: anyone could sign up
================  =============================  ==========================

The devices, ciba, identity and phones apps add ``devices``, ``sign-in``,
``identity`` and ``phones`` when they are installed; ``saml``, identity's
SAML routes, is off by default (they need its extra). Staff's routes
(``auth.admin``) and an OpenID Provider's (``ciba.oidc``) are mounted on
their own, under prefixes of their own.

``account_router(include=["signup"], exclude=["tokens"])`` adds what is
off by default, and leaves out what a project does not want.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final

from fastapi import APIRouter

from domainkit.apps.hooks import HookPoint
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.module_loading import import_string

__all__ = [
    "AccountRoutes",
    "account_router",
    "account_routes",
    "on_account_routes",
]


@dataclass(frozen=True, slots=True)
class AccountRoutes:
    """Routes an app adds to an account's router: their name, and where they are."""

    name: str
    """How a project names them in ``include`` and ``exclude``: ``devices``."""
    router: str
    """The dotted path of their ``APIRouter``:
    ``domainkit.contrib.devices.api.router``."""
    default: bool = True
    """Whether ``account_router()`` mounts them unless excluded. False for
    routes that work only with settings or apps that are missing, or that
    a project must choose (public signup)."""


type RoutesHook = Callable[[], Iterable[AccountRoutes]]

on_account_routes: Final[HookPoint[RoutesHook]] = HookPoint(
    "account routes", arguments=0, awaited=False
)
"""Register a function that names an app's account routes
(``on_account_routes(self.account_routes)`` in ``ready()``)."""


def account_routes() -> dict[str, AccountRoutes]:
    """Every app's account routes, by name, in the order the apps are installed.

    Raises:
        ImproperlyConfigured: Two apps gave routes the same name.
    """
    found: dict[str, AccountRoutes] = {}
    for hook in on_account_routes:
        for routes in hook.call():
            if routes.name in found:
                raise ImproperlyConfigured(
                    f"Two apps name account routes {routes.name!r}: "
                    f"{found[routes.name].router} and {routes.router}."
                )
            found[routes.name] = routes
    return found


def account_router(
    *, include: Iterable[str] = (), exclude: Iterable[str] = ()
) -> APIRouter:
    """A router of the account routes of every installed app (see the module).

    Call it where the project builds its routes, once the domains are set
    up: ``ROOT_ROUTER``'s module is imported then.

    Args:
        include: Routes that are off by default, to mount too.
        exclude: Routes to leave out.

    Raises:
        ImproperlyConfigured: ``include`` or ``exclude`` names routes no
            installed app has.
    """
    known = account_routes()
    wanted, unwanted = set(include), set(exclude)
    if unknown := sorted((wanted | unwanted) - known.keys()):
        raise ImproperlyConfigured(
            f"account_router: no installed app has routes named "
            f"{', '.join(unknown)}; there are {', '.join(known)}."
        )
    router = APIRouter()
    for name, routes in known.items():
        if name not in unwanted and (routes.default or name in wanted):
            router.include_router(import_string(routes.router))
    return router
