"""The twin of a command route: ``POST <path>/authorization`` (ADR 0144).

A rule that asks for an authorization of the act it guards
(``AsksAuthorization``: ``StepUp(for_=...)``) refuses the act until the
client sends one. The twin lets the client ask for it: it takes the
route's path and body, builds the command as the route does, checks what
the act would check with the asking rule passing (the route's other rules,
the aggregate within the caller's reach, the rules on the aggregate), and
answers ``201`` with what the rule's ``ask_authorization`` returns. It
never runs the service. So a caller may only ask for an authorization of
an act they could otherwise make.

The asking rule is found among the route's rules, at the top or inside
``AllOf`` (``&``), at any depth. Inside ``AnyOf`` (``|``) or ``Not``
(``~``) it cannot be decided, and two on one route ask for two
authorizations of one act: both are refused when the route is declared.
"""

import copy
from typing import TYPE_CHECKING, Final

from fastapi import Response, status
from fastapi.routing import APIRoute
from starlette.requests import HTTPConnection

from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import (
    AllOf,
    AnyOf,
    AsksAuthorization,
    BasePermission,
    Not,
    Principal,
    Rule,
)
from domainkit.http.responses import BuiltJSONResponse

from . import reach
from .context import RouteContext, RouteSpec
from .declaring import add_route

if TYPE_CHECKING:
    from .commands import CommandRoute

PATH: Final = "/authorization"
"""What a twin's path adds to its route's."""
SUFFIX: Final = "_authorization"
"""What a twin's name adds to its route's."""
NO_STORE: Final = {"Cache-Control": "no-store"}
"""A twin's answer is the caller's own, and new each time."""


class AuthorizationRoute:
    """The twin of a command route whose rules ask for an authorization."""

    def __init__(
        self,
        route: "CommandRoute",
        rule: AsksAuthorization,
        rules: tuple[BasePermission, ...],
    ) -> None:
        """Describe the twin of ``route``: ``rule`` asks; ``rules`` are checked."""
        self.route = route
        self.rule = rule
        self.rules = rules
        """The route's rules, the asking one replaced by one that lets it pass."""
        self.model = rule.authorization_model()
        spec = route.spec
        self.spec = RouteSpec(
            f"{spec.path}{PATH}",
            "POST",
            f"{spec.name}{SUFFIX}",
            f"Ask for an authorization: {spec.summary or spec.name}",
            spec.action,
            detail=spec.detail,
            body=spec.body,
            returns=self.model,
            status_code=status.HTTP_201_CREATED,
            provided=spec.provided,
            body_limit=spec.body_limit,
            rules=rules,
        )

    @classmethod
    def of(cls, route: "CommandRoute") -> "AuthorizationRoute | None":
        """The twin of ``route``, if one of its rules asks for an authorization.

        Raises:
            ImproperlyConfigured: The asking rule is inside ``AnyOf`` or
                ``Not``; two rules ask; or another route already answers
                ``POST`` on the twin's path.
        """
        spec = route.spec
        router = route.router
        label = (
            f"The command route {spec.method} {router.prefix}{spec.path} "
            f"({router.name_prefix}{spec.name})"
        )
        found: list[AsksAuthorization] = []
        rules = tuple(
            _standing_in(_instance(each), found, label) for each in route.rules
        )
        if not found:
            return None
        if len(found) > 1:
            names = " and ".join(type(each).__name__ for each in found)
            raise ImproperlyConfigured(
                f"{label} has {len(found)} rules that ask for an authorization "
                f"({names}): an act is authorized once, so keep one."
            )
        path = f"{router.prefix}{spec.path}{PATH}"
        taken = next(
            (
                each
                for each in router.routes
                if isinstance(each, APIRoute)
                and each.path == path
                and "POST" in (each.methods or ())
            ),
            None,
        )
        if taken is not None:
            raise ImproperlyConfigured(
                f"{label} asks for an authorization at POST {path}, which "
                f"{taken.name} answers already: give the route a path of its own "
                "(path=...)."
            )
        return cls(route, found[0], rules)

    def declare(self) -> None:
        """Add the twin; one of a route on the collection goes before one item's."""
        router = self.route.router
        add_route(router, self.spec, self.handle)
        if not self.spec.detail:
            router.before_items(router.routes.pop())

    async def handle(self, context: RouteContext) -> Response:
        """Build the command, check what the act would, and ask the rule.

        Raises:
            NotFound: The aggregate is not there, or out of the caller's reach.
            PermissionDenied: A rule refuses the caller the aggregate.
            RuntimeError: The rule answered something else than its model.
        """
        route = self.route
        command = await route.build(context)
        if route.detail:
            uow = context.uow
            async with uow:
                aggregate = await reach.load(
                    route.router, uow, context.request, context.ids
                )
            await reach.check_object(context.request, aggregate, self.rules)
        answer = await self.rule.ask_authorization(context.request, command)
        if not isinstance(answer, self.model):
            raise RuntimeError(
                f"{type(self.rule).__name__}.ask_authorization answered "
                f"{type(answer).__name__}, not the {self.model.__name__} its "
                "authorization_model names."
            )
        return BuiltJSONResponse(
            answer.model_dump(mode="json"),
            status_code=status.HTTP_201_CREATED,
            headers=NO_STORE,
        )


class _Asking(BasePermission):
    """The asking rule, on its twin: any principal who proved who they are.

    The twin is where the authorization is asked for, so the rule cannot
    require it there. An anonymous caller has nothing to be authorized.
    """

    def __init__(self, rule: AsksAuthorization) -> None:
        """Stand for ``rule``."""
        self.rule = rule

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The request named a principal."""
        return user.is_authenticated


def _standing_in(
    rule: BasePermission,
    found: list[AsksAuthorization],
    label: str,
    inside: str | None = None,
) -> BasePermission:
    """``rule``, with a rule that asks for an authorization in it replaced.

    Each such rule is added to ``found``, and replaced by ``_Asking``; an
    ``AllOf`` that holds one is copied with it replaced. Other rules are
    kept as they are.

    Raises:
        ImproperlyConfigured: One is inside ``AnyOf`` or ``Not`` (``inside``).
    """
    if isinstance(rule, AsksAuthorization):
        if inside is not None:
            raise ImproperlyConfigured(
                f"{label} asks for an authorization inside {inside}, where "
                f"{type(rule).__name__} cannot be decided: combine it with the "
                "other rules with & instead."
            )
        found.append(rule)
        return _Asking(rule)
    if isinstance(rule, AllOf):
        before = len(found)
        rules = tuple(_standing_in(each, found, label, inside) for each in rule.rules)
        if len(found) == before:
            return rule
        combined = copy.copy(rule)
        combined.rules = rules
        return combined
    if isinstance(rule, AnyOf):
        for each in rule.rules:
            _standing_in(each, found, label, inside or "AnyOf (|)")
    elif isinstance(rule, Not):
        _standing_in(rule.rule, found, label, inside or "Not (~)")
    return rule


def _instance(rule: Rule) -> BasePermission:
    """A rule instance: a class is instantiated, as ``Require`` does."""
    return rule() if isinstance(rule, type) else rule
