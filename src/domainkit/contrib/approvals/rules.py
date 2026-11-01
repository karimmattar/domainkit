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

from typing import ClassVar, Final

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

from .guards import policy_of
from .models import ApprovalRequest
from .settings import ApprovalsSettings

__all__ = ["IsApprover", "MayApprove", "RequestActions", "configured_rule"]

REFUSED_BY: Final = "domainkit.approvals.refused_by"
"""Where a request keeps the approve rule that refused it (``MayApprove``)."""


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
        """The principal is one the request's policy names.

        Raises:
            PolicyGone: The request's guard or policy no longer exists.
        """
        assert isinstance(obj, ApprovalRequest)  # noqa: S101 - the resource's model.
        _, policy = policy_of(obj)
        if obj.permission is not None:
            return await HasPermissions(obj.permission).has_permission(request, user)
        if obj.group is not None:
            names = {
                getattr(group, "name", group) for group in getattr(user, "groups", ())
            }
            return scopes_of(request) is None and user.is_active and obj.group in names
        rule = _instance(policy.approver_rule)
        return rule is not None and (
            await rule.has_permission(request, user)
            and await rule.has_object_permission(request, user, obj)
        )


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
        """Whether the rule that applies allows the approval.

        A rule that refuses is kept in the request's scope, for ``refusal``.
        """
        assert isinstance(obj, ApprovalRequest)  # noqa: S101 - the resource's model.
        rule = _rule_of(obj)
        if rule is None:
            return True
        allowed = await rule.has_permission(
            request, user
        ) and await rule.has_object_permission(request, user, obj)
        if not allowed:
            request.scope[REFUSED_BY] = rule
        return allowed

    async def refusal(
        self, request: HTTPConnection, user: Principal
    ) -> Exception | None:
        """The refusal of the rule that refused, if it names one.

        ``StepUp``'s ``401 step_up_required`` tells the approver to step up;
        a rule that names none leaves the 403 of ``message``.
        """
        rule: BasePermission | None = request.scope.get(REFUSED_BY)
        return None if rule is None else await rule.refusal(request, user)


def _rule_of(request: ApprovalRequest) -> BasePermission | None:
    """The approve rule of the request's policy, or the configured one.

    Raises:
        PolicyGone: The policy no longer exists: no fallback to rules the
            request was not submitted under.
    """
    _, policy = policy_of(request)
    return _instance(policy.approve_rule) or configured_rule()


def _instance(rule: Rule | None) -> BasePermission | None:
    """``rule``, made from its class if it is one."""
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
