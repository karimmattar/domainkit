"""Who may decide a request: its policy's approvers, and the rule approving asks.

``IsApprover`` lets through the people a request's policy names: holders
of its permission, or members of its group. ``MayApprove`` asks the rule
that approving needs besides: the policy's ``approve_rule``, or
``APPROVALS.APPROVE_RULE``. Any access rule fits, a step-up rule
included::

    APPROVALS = {"APPROVE_RULE": "config.rules.RECENT_SECOND_FACTOR"}

where ``RECENT_SECOND_FACTOR`` is a rule instance of the project's (such
as ``StepUp(max_age=timedelta(minutes=5))``), or a class taking no
arguments. ``ApproveAction`` asks, with ``APPROVALS.APPROVE_ACTION`` on,
for an authorization of the approval itself (ADR 0144), in place of a
step-up window; ``SubmitAction``, ``RejectAction``, and ``CancelAction``
ask for one of submitting, rejecting, and cancelling, with their own
settings on (ADR 0163).
"""

from datetime import datetime
from typing import ClassVar, Final
from uuid import UUID

from fastapi import Request, status
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from domainkit.apps import registry
from domainkit.context import current_request
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import (
    BasePermission,
    HasPermissions,
    Principal,
    Rule,
    scopes_of,
)
from domainkit.http.errors import APIError
from domainkit.resources import HasModelPermission
from domainkit.utils.module_loading import import_string

from .guards import Policy, policy_of
from .models import ApprovalRequest
from .settings import ApprovalsSettings

__all__ = [
    "ApproveAction",
    "CancelAction",
    "IsApprover",
    "MayApprove",
    "RejectAction",
    "RequestActions",
    "SubmitAction",
    "configured_rule",
]

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


class ActsForThemselves(BasePermission):
    """No one acts for the principal: staff acting as a user are refused.

    Makers and approvers are counted as people. Staff acting as a user (an
    impersonation, whose actor is the staff member) would count as that
    user, so one person could make a request and decide it twice.
    """

    message: ClassVar[str] = "Requests are made and decided in one's own name only."

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The request's actor, if one is bound, is the principal."""
        context = current_request()
        actor = None if context is None else context.actor
        return actor is None or actor == str(user.id)


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
            PolicyRemoved: The request's guard or policy no longer exists.
        """
        assert isinstance(obj, ApprovalRequest)  # noqa: S101 - the resource's model.
        _, policy = policy_of(obj)
        rule = rule_instance(policy.approver_rule)
        if obj.permission is None and obj.group is None and rule is not None:
            return await rule.has_permission(
                request, user
            ) and await rule.has_object_permission(request, user, obj)
        return await _named(request, user, obj.permission, obj.group, None)


async def names_approver(
    request: HTTPConnection, user: Principal, policy: Policy
) -> bool:
    """Whether ``policy`` names ``user`` among its approvers.

    By its permission, its group, or its approver rule asked about the
    principal alone (``has_permission``), as ``IsApprover`` asks; the maker
    and the parties are a request's own, and not asked here.
    """
    return await _named(
        request,
        user,
        policy.permission,
        policy.group,
        rule_instance(policy.approver_rule),
    )


async def _named(
    request: HTTPConnection,
    user: Principal,
    permission: str | None,
    group: str | None,
    rule: BasePermission | None,
) -> bool:
    """Whether ``user`` holds ``permission``, is of ``group``, or passes ``rule``.

    A group needs an active principal with credentials not limited to
    scopes.
    """
    if permission is not None:
        return await HasPermissions(permission).has_permission(request, user)
    if group is not None:
        names = {getattr(each, "name", each) for each in getattr(user, "groups", ())}
        return scopes_of(request) is None and user.is_active and group in names
    return rule is not None and await rule.has_permission(request, user)


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


class ApprovalDetailOut(BaseModel):
    """One labelled value the approver is shown."""

    label: str
    value: str


class ApprovalShownOut(BaseModel):
    """What the approver is shown: the request's action and arguments."""

    title: str
    details: list[ApprovalDetailOut]


class ApprovalAuthorizationOut(BaseModel):
    """An authorization of one approval: confirm it, then approve with it."""

    id: UUID
    purpose: str
    shown: ApprovalShownOut
    expires_at: datetime
    methods: list[str]
    """What confirming it must prove: which ways may confirm it."""


class DecisionAction(BasePermission):
    """With its setting on: the act holds an authorization of its own (ADR 0144).

    It asks what ``StepUp(for_=...)`` asks of approvals' action that
    ``setting`` turns on: an ``Action-Authorization`` header holding a
    confirmed authorization of this act, of the caller and of their login.
    ``401 authorization_required`` without one, ``403
    action_not_authorized`` with one that does not hold. It asks about the
    request itself, after the other rules: one that refuses plainly (not an
    approver) answers first. Off, it allows everyone, and its route's twin
    (``POST <the route's path>/authorization``) answers ``404``.
    """

    setting: ClassVar[str]
    """The ``APPROVALS`` key that turns the action on."""

    def __init__(self) -> None:
        """Ask nothing until the setting is on."""
        self._rule: BasePermission | None = None

    def _asking(self) -> BasePermission | None:
        """``StepUp(for_=<the action>)``, while the setting is on."""
        if not getattr(registry.settings(ApprovalsSettings), self.setting):
            return None
        if self._rule is None:
            from domainkit.contrib.auth import StepUp

            from .authorizing import ACTIONS

            self._rule = StepUp(for_=ACTIONS[self.setting])
        return self._rule

    async def has_object_permission(
        self, request: HTTPConnection, user: Principal, obj: object
    ) -> bool:
        """The setting is off, or the request holds an authorization of the act."""
        rule = self._asking()
        return rule is None or await rule.has_permission(request, user)

    async def refusal(
        self, request: HTTPConnection, user: Principal
    ) -> Exception | None:
        """``401 authorization_required`` without the header; else ``403``."""
        rule = self._asking()
        return None if rule is None else await rule.refusal(request, user)

    def authorization_model(self) -> type[BaseModel]:
        """``{"id", "purpose", "shown", "expires_at", "methods"}``: the twin's body."""
        return ApprovalAuthorizationOut

    async def ask_authorization(self, request: Request, command: object) -> BaseModel:
        """Store an authorization of the act ``command`` makes; the twin's body.

        It is refused as ``request_action_authorization`` refuses, and as
        the act itself would be refused.

        Raises:
            APIError: 404 ``not_found``, while the setting is off.
        """
        if self._asking() is None:
            raise APIError(status.HTTP_404_NOT_FOUND, "Not Found", code="not_found")
        from .authorizing import ask

        return ApprovalAuthorizationOut.model_validate(
            await ask(request, self.setting, command)
        )


class ApproveAction(DecisionAction):
    """With ``APPROVALS.APPROVE_ACTION`` on: the approval holds its own authorization.

    An authorization of approving this request with this fingerprint
    (``approvals.approve``); its twin is ``POST
    /requests/{id}/approve/authorization``.
    """

    setting: ClassVar[str] = "APPROVE_ACTION"


class RejectAction(DecisionAction):
    """With ``APPROVALS.REJECT_ACTION`` on: the rejection holds its own authorization.

    An authorization of rejecting this request for this reason
    (``approvals.reject``, ADR 0163); its twin is ``POST
    /requests/{id}/reject/authorization``.
    """

    setting: ClassVar[str] = "REJECT_ACTION"


class CancelAction(DecisionAction):
    """With ``APPROVALS.CANCEL_ACTION`` on: the withdrawal holds its own authorization.

    An authorization of cancelling this request for this reason
    (``approvals.cancel``, ADR 0163); its twin is ``POST
    /requests/{id}/cancel/authorization``.
    """

    setting: ClassVar[str] = "CANCEL_ACTION"


class SubmitAction(DecisionAction):
    """With ``APPROVALS.SUBMIT_ACTION`` on: the submission holds its own authorization.

    An authorization of submitting this command with these arguments
    (``approvals.submit``, ADR 0163), asked of the request before its body
    is read; its twin is ``POST /requests/authorization``.
    """

    setting: ClassVar[str] = "SUBMIT_ACTION"

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The setting is off, or the request holds an authorization of a submission."""
        return await self.has_object_permission(request, user, None)


def _rule_of(request: ApprovalRequest) -> BasePermission | None:
    """The approve rule of the request's policy, or the configured one.

    Raises:
        PolicyRemoved: The policy no longer exists: no fallback to rules the
            request was not submitted under.
    """
    _, policy = policy_of(request)
    return rule_instance(policy.approve_rule) or configured_rule()


def rule_instance(rule: Rule | None) -> BasePermission | None:
    """``rule``, made from its class if it is one."""
    return rule() if isinstance(rule, type) else rule


class RequestActions:
    """The rules of each route of the requests (``ModelActions``' protocol)."""

    def rules_for(self, action: str) -> tuple[Rule, ...]:
        """View to read, add to submit and cancel, approver to decide.

        Submitting, deciding, and cancelling are in one's own name only
        (``ActsForThemselves``). Each decision asks last for an
        authorization of itself, with its setting on: ``ApproveAction``,
        ``RejectAction``, ``CancelAction``.
        """
        if action == "approve":
            return (ActsForThemselves(), IsApprover(), MayApprove(), ApproveAction())
        if action == "reject":
            return (ActsForThemselves(), IsApprover(), RejectAction())
        if action == "cancel":
            return (
                ActsForThemselves(),
                HasModelPermission("add", ApprovalRequest),
                CancelAction(),
            )
        return (HasModelPermission(action, ApprovalRequest),)
