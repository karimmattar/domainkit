"""The twin of a command route: ``POST <path>/authorization`` (ADR 0144).

A rule that asks for an authorization of the act it guards
(``AsksAuthorization``: ``StepUp(for_=...)``) refuses the act until the
client sends one. The twin lets the client ask for it: it takes the
route's path and body, builds the command as the route does, checks what
the act would check with the asking rule passing (the route's other rules,
the aggregate within the caller's reach, the rules on the aggregate), and
answers ``201`` with what the rule's ``ask_authorization`` returns (given
the route's unit of work, when it takes one: ADR 0161). It never runs the
service. So a caller may only ask for an authorization of an act they
could otherwise make.

The asking rule is found among the route's rules, at the top or inside
``AllOf`` (``&``), at any depth. Inside ``AnyOf`` (``|``) or ``Not``
(``~``) it cannot be decided, and two on one route ask for two
authorizations of one act: both are refused when the route is declared.
So is a service the rule says would not spend what it asks for (its
``service_problem``): ``StepUp(for_=...)`` on a use case left unguarded.

On an act on one aggregate the twin holds ``If-Match`` as the act does
(ADR 0178): the aggregate at another version than the one it names is
``412``, before the rule is asked, and again where the rule reads in the
route's unit of work; a rule whose method takes ``if_match`` is given the
header, which an action's digest then covers. A page reads one version,
so a twin takes one tag (``422`` for several). Without ``If-Match``, as
before. An action that requires ``If-Match`` on an act on the collection,
which reads none, is refused when the route is declared.

Where routes of one path ask by several methods (a ``PATCH`` and a
``DELETE`` of one item), each twin's path names its route's method:
``<path>/authorization/patch``, ``<path>/authorization/delete`` (ADR 0162).
A twin runs none of its route's ``dependencies``: a limit there counts
acts, not asks. It runs its route's ``before_rules``, which come before
the rules it runs too. What runs on both (``before_rules``, the rules,
``provide``) tells a twin's request apart with ``asking_authorization``.
"""

import copy
import dataclasses
import inspect
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

from fastapi import Response, status
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from domainkit.ddd.preconditions import Expectation
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

from ..conditional import asked_versions
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
    on it; its ``before_rules`` do. Known once the request is routed: false
    in a middleware.
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
        self.lends = _takes(rule, "uow")
        """Whether the rule reads in the route's unit of work (ADR 0161)."""
        self.passes = _takes(rule, "if_match")
        """Whether the rule is given the request's ``If-Match`` (ADR 0178)."""
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
            body_limit_per_request=spec.body_limit_per_request,
            rules=rules,
            before_rules=spec.before_rules,
            conditional=True,
            binds_if_match=self.passes,
        )

    @classmethod
    def of(cls, route: "CommandRoute") -> "AuthorizationRoute | None":
        """The twin of ``route``, if one of its rules asks for an authorization.

        The twin of a route of a path whose other routes ask too names the
        route's method in its path, and so do theirs, from then on.

        Raises:
            ImproperlyConfigured: The asking rule is inside ``AnyOf`` or
                ``Not``; two rules ask; the rule says the route's service
                would not spend what it asks for; its action requires
                ``If-Match`` on an act on the collection; or another route
                already answers ``POST`` on the twin's path.
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
        check = getattr(found[0], "service_problem", None)
        if check is not None and (problem := check(route.service)) is not None:
            raise ImproperlyConfigured(
                f"{label} asks for an authorization of its act, but {problem}."
            )
        action = getattr(found[0], "action", None)
        if not route.detail and getattr(action, "require_if_match", False) is True:
            raise ImproperlyConfigured(
                f"{label} acts on the collection, which reads no If-Match, and its "
                "action requires one (require_if_match=True): no ask would hold."
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

    def expected(self, context: RouteContext, versions: frozenset[int]) -> Expectation:
        """The aggregate the path names, expected at one of ``versions``."""
        router = self.route.router
        return Expectation(router.model, context.ids[router.lookup], versions)

    @contextmanager
    def held(
        self, context: RouteContext, versions: frozenset[int] | None
    ) -> Iterator[None]:
        """Hold the route's unit of work to ``versions``, lent to the rule.

        What ``show`` and ``binds`` load of the aggregate through it is
        checked, so a change made since the check above is ``412`` before
        anything is stored. The expectation counts as checked: the twin
        checked the aggregate itself, so ``request_action_authorization``
        may cover the versions. Without ``If-Match``, or a unit of work the
        rule does not take, nothing is held.
        """
        if versions is None or not self.lends:
            yield
            return
        router = self.route.router
        uow = context.uow
        with uow.expect(router.model, context.ids[router.lookup], versions) as held:
            held.checked = True
            yield

    def path(self, *, by_method: bool) -> str:
        """Its path in the router: ``<path>/authorization``, or ``.../<method>``."""
        spec = self.route.spec
        path = f"{spec.path}{PATH}"
        return f"{path}/{spec.method.lower()}" if by_method else path

    async def handle(self, context: RouteContext) -> Response:
        """Build the command, check what the act would, and ask the rule.

        On one aggregate, ``If-Match`` holds it as the act's write is held:
        checked once the rules allowed, and again in the route's unit of
        work, which the rule reads in (``held``).

        Raises:
            NotFound: The aggregate is not there, or out of the caller's reach.
            PermissionDenied: A rule refuses the caller the aggregate.
            PreconditionFailed: The aggregate is not at the version
                ``If-Match`` names (412).
            RequestValidationError: ``If-Match`` lists several tags, on
                one line or several (422).
            RuntimeError: The rule answered something else than its model,
                or the aggregate loaded is not the one ``If-Match`` was to
                be checked against.
        """
        route = self.route
        header = context.if_match
        lines = context.request.headers.getlist("if-match")
        if len(lines) > 1:  # One list (RFC 9110, 5.3): a page sends one tag.
            header = ", ".join(lines)
        versions = asked_versions(header)
        command = await route.build(context)
        if route.detail:
            uow = context.uow
            async with uow:
                aggregate = await reach.load(
                    route.router, uow, context.request, context.ids
                )
            await reach.check_object(context.request, aggregate, self.rules)
            if versions is not None:
                expected = self.expected(context, versions)
                expected.check(aggregate)
                if not expected.checked:  # Never a pass unchecked, as conditionally.
                    raise RuntimeError(
                        f"The twin loaded {aggregate!r}, not the "
                        f"{expected.model.__name__} {expected.id} the path names, "
                        "so If-Match could not be checked."
                    )
        lent: dict[str, object] = {"uow": context.uow} if self.lends else {}
        if self.passes:
            lent["if_match"] = header
        ask: Callable[..., Awaitable[BaseModel]] = self.rule.ask_authorization
        with self.held(context, versions):
            answer = await ask(context.request, command, **lent)
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


def _takes(rule: AsksAuthorization, name: str) -> bool:
    """Whether ``rule.ask_authorization`` takes ``name`` by name: ``uow``, ``if_match``.

    One of 2.4 takes neither; one of 2.5 to 2.7 no ``if_match``.
    """
    try:
        parameters = inspect.signature(rule.ask_authorization).parameters
    except (TypeError, ValueError):  # No signature to read: called as in 2.4.
        return False
    found = parameters.get(name)
    return found is not None and found.kind in BY_NAME


BY_NAME: Final = frozenset(
    {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
)
"""The kinds of parameter ``uow=`` and ``if_match=`` can be given to."""


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
