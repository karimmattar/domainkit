"""Who may call a resource's routes: one rule, or model permissions by action.

Every route of a resource has an action: ``view`` (list and read), ``add``
(create), ``change`` (commands and updates), or ``delete``. A resource's
``rule`` is a rule for every route, or ``ModelActions``, which asks for
Django's model permission of each route's action, so a command posted to
``/invoices/{id}/approve`` needs ``billing.change_invoice``, not the
``add`` a ``POST`` would need under ``ModelPermissions``::

    ResourceRouter("/invoices", model=Invoice, rule=ModelActions(Invoice), ...)
    ResourceRouter("/users", ..., rule=ModelActions(get_user_model, IsAdminUser()))

The permission keys name installed models (``domainkit.db.models``); the
principal answers them (``Principal.ahas_perms``), whatever authenticates
requests. ``MemberActions`` asks for the same keys among those a member
holds in the tenant being served, and no others (ADR 0170). On a
resource whose items are tenants, its collection's routes serve none:
``outside`` says who may list and create there, and a ``scope`` of the
project's keeps each caller to the tenants they belong to, as no member
rule narrows a list::

    ResourceRouter("/companies", ..., tenant=PathTenant("company_id"),
                   rule=MemberActions(Company, outside=IsAuthenticated()),
                   scope=companies_of_caller)

An action of the domain's own whose key names no model's permission (an
invitation posted to a company) is named whole by ``keys`` (ADR 0177)::

    MemberActions(Company, keys={"invite": "organizations.add_membership"})
"""

import re
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import ClassVar, Final, Protocol, runtime_checkable

from starlette.requests import HTTPConnection

from domainkit.db.models import model_key
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import (
    AllOf,
    BasePermission,
    HasPermissions,
    Principal,
    Rule,
    scopes_of,
)
from domainkit.tenancy import HasMemberPermissions, path_tenant

VIEW: Final = "view"
ADD: Final = "add"
CHANGE: Final = "change"
DELETE: Final = "delete"
ACTIONS: Final = frozenset({VIEW, ADD, CHANGE, DELETE})
"""The actions every model has a permission of, unless its ``Meta`` says."""
PERMISSION_KEY: Final = re.compile(r"\w+\.\w+")
"""A permission's key: ``<domain>.<codename>``."""

type ModelLike = type | str | Callable[[], type]
"""A mapped class, its content type (``"billing.invoice"``), or a function
returning the class (resolved on each request, as for a swappable user)."""


@runtime_checkable
class Actions(Protocol):
    """Rules that depend on a route's action (``view``, ``add``, ...)."""

    def rules_for(self, action: str) -> Sequence[Rule]:
        """The rules a route taking ``action`` checks, in order."""
        ...


class HasModelPermission(BasePermission):
    """Users granted ``<domain>.<action>_<model>``, the model resolved per request.

    It asks ``HasPermissions`` for the key, so whatever that rule checks
    (the backends, and any limit on the credentials) applies alike: it
    checks scopes, so credentials limited to the key pass it (WEB-15).
    ``key`` names the key whole instead, for an action whose key names no
    model's permission (``organizations.add_membership``).
    """

    bounds_scopes: ClassVar[bool] = True
    key: str | None = None
    """The key asked whole, instead of the model's (``keys`` of ``ModelActions``)."""

    def __init__(
        self, action: str, model: ModelLike, *, key: str | None = None
    ) -> None:
        """Check ``action`` on ``model``, or ``key`` (New in 2.7).

        Raises:
            ValueError: ``key`` is not a permission's key.
        """
        self.action = action
        self.model = model
        self.key = None if key is None else _checked_key(key)

    def permission(self) -> str:
        """The permission's key: ``billing.change_invoice``, or ``key``."""
        if self.key is not None:
            return self.key
        domain, name = content_type_of(self.model)
        return f"{domain}.{self.action}_{name}"

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The user holds the permission."""
        return await HasPermissions(self.permission()).has_permission(request, user)


class ModelActions:
    """Model permissions by each route's action, after any ``rules`` given.

    ``ModelActions(Invoice)`` needs ``billing.view_invoice`` to list,
    ``billing.add_invoice`` to create, and so on;
    ``ModelActions(Invoice, IsAdminUser())`` needs staff as well. ``keys``
    names the key of an action whole, where the model's would not do:
    ``keys={"invite": "organizations.add_membership"}`` asks a route of
    the action ``invite`` for that key, not ``companies.invite_company``.
    Once ``keys`` is given, a route of an action other than ``view``,
    ``add``, ``change`` or ``delete`` that it does not name is refused
    when declared, so a misspelt action never asks a key no one holds.
    """

    keys: Mapping[str, str] = MappingProxyType({})
    """The key of each action named whole, by action."""

    def __init__(
        self, model: ModelLike, *rules: Rule, keys: Mapping[str, str] | None = None
    ) -> None:
        """Ask for ``model``'s permissions, after ``rules``; ``keys`` by action.

        Raises:
            ValueError: A key of ``keys`` is not a permission's key.
        """
        self.model = model
        self.rules = rules
        self.keys = _checked_keys(keys)

    def rules_for(self, action: str) -> Sequence[Rule]:
        """``rules``, then the model permission of ``action``, or its key.

        Raises:
            ImproperlyConfigured: ``keys`` is given and names neither
                ``action`` nor is it one every model has.
        """
        key = _key_of(self, action)
        return (*self.rules, HasModelPermission(action, self.model, key=key))


class HasMemberModelPermission(HasModelPermission):
    """Members granted ``<domain>.<action>_<model>`` in the tenant being served.

    It asks ``HasMemberPermissions`` for the key: what the caller holds as a
    member of the tenant counts, and what they hold everywhere does not.
    """

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The member holds the permission in the tenant."""
        return await HasMemberPermissions(self.permission()).has_permission(
            request, user
        )


class _ByPath(HasMemberModelPermission):
    """A member's key, on a route whose path named the tenant (``PathTenant``).

    Beside ``outside``, a member's keys count only where the resource's
    tenant was taken from the path: on its collection, a tenant a
    middleware serves (a header's, a subdomain's) decides nothing.
    """

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The path named the tenant, and the member holds the key there."""
        return path_tenant() is not None and await super().has_permission(request, user)


class _ServedAsNone(AllOf):
    """``outside``'s rules, on a route whose path named no tenant; refused else.

    It asks only those rules about an object, and checks scopes: credentials
    limited to scopes pass it only where those rules check them. So, beside
    a member's rule (``|``), it neither refuses every object nor keeps
    scoped credentials from a tenant's routes. A tenant a middleware serves
    does not count: the collection's routes are the resource's, whatever
    the request's tenant.
    """

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """No path named a tenant, and every rule allows the request, scopes too."""
        if path_tenant() is not None:
            return False
        if scopes_of(request) is not None and not super().checks_scopes():
            return False
        return await super().has_permission(request, user)

    def checks_scopes(self) -> bool:
        """Always: it refuses scoped credentials that its rules do not check."""
        return True


class MemberActions:
    """Members' permissions by each route's action, after any ``rules`` given.

    On a route served as a tenant, ``MemberActions(Company)`` needs
    ``companies.view_company`` to read, ``companies.change_company`` to run
    a command, and so on, among the keys the caller holds as a member of
    the tenant (``TENANCY.MEMBER_PERMISSIONS``) alone: a member who is also
    staff acts there with their role, not their staff permissions (ADR
    0170). A route served as no tenant is refused, unless ``outside`` says
    who may call it. A ``ResourceRouter`` with ``tenant=`` serves no tenant
    on its collection's routes. A rule given as ``outside`` decides only
    the ``view`` and ``add`` routes there (the list, queries on the
    collection, creation); a route of any other action there (a command
    that changes or deletes from the collection) is refused, unless
    ``outside`` is ``Actions``, such as ``ModelActions``, which names the
    rules of each action itself. With ``outside``, a member's keys count
    only where the path named the tenant (``path_tenant``), and ``outside``
    rules wherever it did not, whatever tenant a middleware serves.

    ``keys`` names the key of an action of the domain's own whole, as
    ``ModelActions`` takes it: an invitation posted to a company, under
    ``keys={"invite": "organizations.add_membership"}``, asks the member
    for that key (New in 2.7). ``outside``, as ``Actions``, names its own:
    it does not take these.
    """

    keys: Mapping[str, str] = MappingProxyType({})
    """The key of each action named whole, by action."""

    def __init__(
        self,
        model: ModelLike,
        *rules: Rule,
        outside: "Rule | Actions | None" = None,
        keys: Mapping[str, str] | None = None,
    ) -> None:
        """Ask for ``model``'s permissions among a member's keys, after ``rules``.

        Raises:
            ValueError: A key of ``keys`` is not a permission's key.
        """
        self.model = model
        self.rules = rules
        self.outside = outside
        self.keys = _checked_keys(keys)

    def rules_for(self, action: str) -> Sequence[Rule]:
        """``rules``, then the member's permission of ``action``, or ``outside``'s.

        Raises:
            ImproperlyConfigured: ``keys`` is given and names neither
                ``action`` nor is it one every model has.
        """
        given = self.outside
        key = _key_of(self, action)
        if given is None:
            return (*self.rules, HasMemberModelPermission(action, self.model, key=key))
        member = _ByPath(action, self.model, key=key)
        outside = (
            rules_for(given, action)
            if isinstance(given, Actions) or action in {VIEW, ADD}
            else ()
        )
        if not outside:
            return (*self.rules, member)
        return (*self.rules, member | _ServedAsNone(*outside))


def rules_for(rule: Rule | Actions | None, action: str) -> tuple[Rule, ...]:
    """The rules a route taking ``action`` checks; none if ``rule`` is None."""
    if rule is None:
        return ()
    if isinstance(rule, Actions):
        return tuple(rule.rules_for(action))
    return (rule,)


def _key_of(actions: ModelActions | MemberActions, action: str) -> str | None:
    """The key ``actions`` names for ``action``; None for the model's own.

    Raises:
        ImproperlyConfigured: ``keys`` is given, and names neither ``action``
            nor is it ``view``, ``add``, ``change`` or ``delete``: a route of
            a misspelt action, or of one ``keys`` misspells.
    """
    keys = actions.keys
    if not keys or action in keys or action in ACTIONS:
        return keys.get(action)
    from domainkit.utils.text import did_you_mean  # For the message only.

    raise ImproperlyConfigured(
        f"{type(actions).__name__}: the action {action!r} is not in keys"
        f"{did_you_mean(action, keys)}; name its key there, or declare the "
        "route with view, add, change, or delete."
    )


def _checked_keys(keys: Mapping[str, str] | None) -> Mapping[str, str]:
    """``keys``, each a permission's key, read-only.

    Raises:
        ValueError: One is not a permission's key.
    """
    return MappingProxyType(
        {action: _checked_key(key) for action, key in (keys or {}).items()}
    )


def _checked_key(key: str) -> str:
    """``key``, if it is a permission's key (``<domain>.<codename>``).

    Raises:
        ValueError: It is not.
    """
    if not isinstance(key, str) or not PERMISSION_KEY.fullmatch(key):
        raise ValueError(
            f"{key!r} is not a permission's key: name it as <domain>.<codename>, "
            "such as 'organizations.add_membership'."
        )
    return key


def content_type_of(model: ModelLike) -> tuple[str, str]:
    """``(domain, model)`` of a mapped class, a content type, or a function.

    Raises:
        LookupError: The class is not mapped in an installed domain.
    """
    if isinstance(model, str):
        domain, _, name = model.partition(".")
        return domain, name
    cls = model if isinstance(model, type) else model()
    return model_key(cls)
