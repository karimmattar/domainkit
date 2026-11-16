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

Where routes of one path ask by several methods (a ``PATCH`` and a
``DELETE`` of one item), each twin's path names its route's method:
``<path>/authorization/patch``, ``<path>/authorization/delete`` (ADR 0162).
A twin runs none of its route's ``dependencies``: a limit there counts
acts, not asks. What runs on both (the rules, ``provide``) tells a twin's
request apart with ``asking_authorization``.
"""

import copy
import dataclasses
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
    from . import ResourceRouter
    from .commands import CommandRoute

PATH: Final = "/authorization"
"""What a twin's path adds to its route's."""
SUFFIX: Final = "_authorization"
"""What a twin's name adds to its route's."""
NO_STORE: Final = {"Cache-Control": "no-store"}
"""A twin's answer is the caller's own, and new each time."""
TWIN: Final = "domainkit_twin"
"""The attribute of a twin's endpoint that holds the twin."""


def asking_authorization(connection: HTTPConnection) -> bool:
    """Whether the request is to a twin: it asks for an authorization of an act.

    A twin makes no act. Its route's rules and ``provide`` run on it as on
    the act, so code among them that counts acts (a limit, a quota) skips
    the request where this is true. Its route's ``dependencies`` never run
    on it. Known once the request is routed: false in a middleware.
    """
    return hasattr(connection.scope.get("endpoint"), TWIN)


class AuthorizationRoute:
    """The twin of a command route whose rules ask for an authorization."""

    def __init__(
        self,
        route: "CommandRoute",
        rule: AsksAuthorization,
        rules: tuple[BasePermission, ...],
        *,
        by_method: bool = False,
    ) -> None:
        """Describe the twin of ``route``: ``rule`` asks; ``rules`` are checked.

        ``by_method`` names the route's method in the twin's path: other
        routes of its path ask too.
        """
        self.route = route
        self.rule = rule
        self.rules = rules
        """The route's rules, the asking one replaced by one that lets it pass."""
        self.model = rule.authorization_model()
        self.by_method = by_method
        """Whether its path names its route's method."""
        self.moved: list[AuthorizationRoute] = []
        """Twins of other methods of the route's path, which then name theirs."""
        self.added: APIRoute | None = None
        """The route FastAPI declared, once it is."""
        spec = route.spec
        self.spec = RouteSpec(
            self.path(by_method=by_method),
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

        The twin of a route of a path whose other routes ask too names the
        route's method in its path, and so do theirs, from then on.

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
        others = [each for each in _twins(router) if each.route.spec.path == spec.path]
        twin = cls(route, found[0], rules, by_method=bool(others))
        moved = [each for each in others if not each.by_method]
        same = next(
            (each.added for each in others if each.route.spec.method == spec.method),
            None,
        )
        for path in (twin.spec.path, *(each.path(by_method=True) for each in moved)):
            full = f"{router.prefix}{path}"
            if (taken := same or _taken(router, full, moved)) is not None:
                raise ImproperlyConfigured(
                    f"{label} asks for an authorization at POST {full}, which "
                    f"{taken.name} answers already: give the route a path of its "
                    "own (path=...)."
                )
        twin.moved = moved
        return twin

    def declare(self) -> None:
        """Add the twin, after moving those of its path's other methods to theirs."""
        for each in self.moved:
            each.move()
        self.add()

    def add(self) -> None:
        """Add the route; one of a route on the collection goes before one item's."""
        router = self.route.router
        self.added = add_route(router, self.spec, self.handle)
        setattr(self.added.endpoint, TWIN, self)
        if not self.spec.detail:
            router.before_items(router.routes.pop())

    def move(self) -> None:
        """Take the route to the path that names its route's method."""
        router = self.route.router
        added = self.added
        assert added is not None  # noqa: S101 - moved once declared.
        router.routes.remove(added)
        router.operations[:] = [
            each for each in router.operations if each.name != added.name
        ]
        self.by_method = True
        self.spec = dataclasses.replace(self.spec, path=self.path(by_method=True))
        self.add()

    def path(self, *, by_method: bool) -> str:
        """Its path in the router: ``<path>/authorization``, or ``.../<method>``."""
        spec = self.route.spec
        path = f"{spec.path}{PATH}"
        return f"{path}/{spec.method.lower()}" if by_method else path

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


def _twins(router: "ResourceRouter") -> list[AuthorizationRoute]:
    """The twins ``router`` holds, in order."""
    return [
        twin
        for each in router.routes
        if isinstance(each, APIRoute)
        and isinstance(twin := getattr(each.endpoint, TWIN, None), AuthorizationRoute)
    ]


def _taken(
    router: "ResourceRouter", path: str, moving: list[AuthorizationRoute]
) -> APIRoute | None:
    """The route that answers ``POST`` at ``path``, but those of twins ``moving``."""
    leaving = {id(each.added) for each in moving}
    return next(
        (
            each
            for each in router.routes
            if isinstance(each, APIRoute)
            and each.path == path
            and "POST" in (each.methods or ())
            and id(each) not in leaving
        ),
        None,
    )
