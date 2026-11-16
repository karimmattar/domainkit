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
"""

from collections.abc import Callable, Sequence
from typing import ClassVar, Final, Protocol, runtime_checkable

from starlette.requests import HTTPConnection

from domainkit.db.models import model_key
from domainkit.http.access import (
    AllOf,
    BasePermission,
    HasPermissions,
    Principal,
    Rule,
    scopes_of,
)
from domainkit.tenancy import HasMemberPermissions, current_tenant

VIEW: Final = "view"
ADD: Final = "add"
CHANGE: Final = "change"
DELETE: Final = "delete"

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
    """

    bounds_scopes: ClassVar[bool] = True

    def __init__(self, action: str, model: ModelLike) -> None:
        """Check ``action`` on ``model``."""
        self.action = action
        self.model = model

    def permission(self) -> str:
        """The permission's key: ``billing.change_invoice``."""
        domain, name = content_type_of(self.model)
        return f"{domain}.{self.action}_{name}"

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The user holds the permission."""
        return await HasPermissions(self.permission()).has_permission(request, user)


class ModelActions:
    """Model permissions by each route's action, after any ``rules`` given.

    ``ModelActions(Invoice)`` needs ``billing.view_invoice`` to list,
    ``billing.add_invoice`` to create, and so on;
    ``ModelActions(Invoice, IsAdminUser())`` needs staff as well.
    """

    def __init__(self, model: ModelLike, *rules: Rule) -> None:
        """Ask for ``model``'s permissions, after ``rules``."""
        self.model = model
        self.rules = rules

    def rules_for(self, action: str) -> Sequence[Rule]:
        """``rules``, then the model permission of ``action``."""
        return (*self.rules, HasModelPermission(action, self.model))


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


class _ServedAsNone(AllOf):
    """``outside``'s rules, on a request served as no tenant; refused in one.

    It asks only those rules about an object, and checks scopes: credentials
    limited to scopes pass it only where those rules check them. So, beside
    a member's rule (``|``), it neither refuses every object nor keeps
    scoped credentials from a tenant's routes.
    """

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """No tenant is served, and every rule allows the request, scopes too."""
        if current_tenant() is not None:
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
    rules of each action itself.
    """

    def __init__(
        self, model: ModelLike, *rules: Rule, outside: "Rule | Actions | None" = None
    ) -> None:
        """Ask for ``model``'s permissions among a member's keys, after ``rules``."""
        self.model = model
        self.rules = rules
        self.outside = outside

    def rules_for(self, action: str) -> Sequence[Rule]:
        """``rules``, then the member's permission of ``action``, or ``outside``'s."""
        member = HasMemberModelPermission(action, self.model)
        given = self.outside
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
