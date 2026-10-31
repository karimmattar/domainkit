"""Who may decide a request: its policy's approvers, and the rule approving asks.

``IsApprover`` lets through the people a request's policy names: holders
of its permission, or members of its group. ``MayApprove`` asks the rule
that approving needs besides: the policy's ``approve_rule``, or
``APPROVALS.APPROVE_RULE``. Any access rule fits, a step-up rule
included::

    APPROVALS = {"APPROVE_RULE": "config.rules.RECENT_SECOND_FACTOR"}

where ``RECENT_SECOND_FACTOR`` is a rule instance of the project's (such
as ``StepUp(max_age=timedelta(minutes=5))``), or a class taking no
arguments.
"""

from typing import ClassVar

from starlette.requests import HTTPConnection

from domainkit.apps import registry
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import (
    BasePermission,
    HasPermissions,
    Principal,
    Rule,
    scopes_of,
)
from domainkit.resources import HasModelPermission
from domainkit.utils.module_loading import import_string

from .guards import guards
from .models import ApprovalRequest
from .settings import ApprovalsSettings

__all__ = ["IsApprover", "MayApprove", "RequestActions", "configured_rule"]


def configured_rule() -> BasePermission | None:
    """``APPROVALS.APPROVE_RULE``, imported: a rule instance, or None.

    Raises:
        ImproperlyConfigured: The path names neither a rule nor a rule class.
    """
    path = registry.settings(ApprovalsSettings).APPROVE_RULE
    if path is None:
        return None
    try:
        found = import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(
            f"APPROVALS.APPROVE_RULE {path!r} cannot be imported: {exc}"
        ) from None
    if isinstance(found, type) and issubclass(found, BasePermission):
        return found()
    if isinstance(found, BasePermission):
        return found
    raise ImproperlyConfigured(
        f"APPROVALS.APPROVE_RULE {path!r} is not an access rule (BasePermission)."
    )


class IsApprover(BasePermission):
    """One of the people a request's policy lets decide it.

    Whoever holds the policy's permission (as the credentials' scopes
    allow), or, for a policy of a group, an active member of it, with
    credentials not limited to scopes. The maker is refused by the
    service, whatever their permissions.
    """

    message: ClassVar[str] = "You are not one of the approvers of this request."
    bounds_scopes: ClassVar[bool] = True

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """Only principals who proved who they are decide."""
        return user.is_authenticated and user.is_active

    async def has_object_permission(
        self, request: HTTPConnection, user: Principal, obj: object
    ) -> bool:
        """The principal is one the request's policy names."""
        assert isinstance(obj, ApprovalRequest)  # noqa: S101 - the resource's model.
        if obj.permission is not None:
            return await HasPermissions(obj.permission).has_permission(request, user)
        names = {getattr(group, "name", group) for group in getattr(user, "groups", ())}
        return scopes_of(request) is None and user.is_active and obj.group in names


class MayApprove(BasePermission):
    """The rule approving a request asks for besides its approvers: a step-up, say.

    The request's policy's ``approve_rule`` if its guard still has the
    policy and it names one, else ``APPROVALS.APPROVE_RULE``; with
    neither, anyone ``IsApprover`` allows. The rule is asked both
    whether it allows the request and the object.
    """

    message: ClassVar[str] = "You cannot approve this request now."

    async def has_object_permission(
        self, request: HTTPConnection, user: Principal, obj: object
    ) -> bool:
        """Whether the rule that applies allows the approval."""
        assert isinstance(obj, ApprovalRequest)  # noqa: S101 - the resource's model.
        rule = _rule_of(obj)
        if rule is None:
            return True
        return await rule.has_permission(
            request, user
        ) and await rule.has_object_permission(request, user, obj)


def _rule_of(request: ApprovalRequest) -> BasePermission | None:
    """The approve rule of the request's policy, or the configured one."""
    registered = guards().get(request.action)
    policy = None if registered is None else registered.policy_named(request.policy)
    rule = None if policy is None else policy.approve_rule
    if rule is None:
        return configured_rule()
    return rule() if isinstance(rule, type) else rule


class RequestActions:
    """The rules of each route of the requests (``ModelActions``' protocol)."""

    def rules_for(self, action: str) -> tuple[Rule, ...]:
        """View to read, add to submit and cancel, approver to decide."""
        if action == "approve":
            return (IsApprover(), MayApprove())
        if action == "reject":
            return (IsApprover(),)
        if action == "cancel":
            return (HasModelPermission("add", ApprovalRequest),)
        return (HasModelPermission(action, ApprovalRequest),)
