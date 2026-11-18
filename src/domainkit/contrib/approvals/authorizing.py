"""Deciding with an authorization of its own: approvals' actions.

With ``APPROVALS.APPROVE_ACTION`` on (ADR 0144), an approver confirms each
approval as one act: the request's id and the fingerprint they state are
the act's arguments, the request is its subject
(``approvals.request:<id>``), and the user is shown the request's action
and arguments, built here from the stored request. ``approve_request``
spends the authorization through ``approve_authorized``, with the
approval's commit where the requests share auth's database.

``SUBMIT_ACTION``, ``REJECT_ACTION``, and ``CANCEL_ACTION`` do the same
for submitting at ``POST /requests`` (``approvals.submit``, on
``approvals.action:<action>``), rejecting (``approvals.reject``), and
cancelling (``approvals.cancel``), each showing the request's action and
arguments, and the reason given (ADR 0163).

What a request shows is its guard's ``show``, or its arguments by name,
laid out on a phone's screen, a list over as many lines as it takes. With
``AUTH.AUTHORIZATIONS.WHOLE`` on, submitting and approving refuse what
does not fit whole (ADR 0173); rejecting and cancelling cut and count it,
and refuse a reason longer than a line (since 2.8, ADR 0180). A reason
given, and what a guard's ``show`` marks ``Typed``, are checked as typed
text: with ``AUTH.AUTHORIZATIONS.TYPED_LINKS`` refused, one that holds a
link is refused. Rejecting and cancelling check their own reason, never
what the request typed.

Only imported with one of them on, and so with ``domainkit.contrib.auth``
installed (``ApprovalsConfig.ready`` refuses it otherwise).
"""

from collections.abc import Iterable, Mapping
from typing import Any, Final, cast

from starlette.requests import HTTPConnection

from domainkit.apps import registry
from domainkit.contrib.auth import AuthSettings
from domainkit.contrib.auth.actions import (
    Action,
    ActionAuthorizationOut,
    Shown,
    register_action,
    request_action_authorization,
    requires_authorization,
)
from domainkit.contrib.auth.screens import Typed, fitted, laid_out, on_typed_text
from domainkit.ddd import UnitOfWork
from domainkit.i18n import gettext, gettext_noop, ngettext
from domainkit.money import Money

from .guards import Guard, guard_for, policy_of
from .models import ApprovalRequest
from .services import (
    ApproveRequest,
    CancelRequest,
    RejectRequest,
    SubmitRequest,
    check_approval,
    check_cancellation,
    check_rejection,
    check_submission,
    record_approval,
    record_cancellation,
    record_rejection,
    record_submission,
)
from .settings import ApprovalsSettings

PURPOSE: Final = "approvals.approve"
"""The action approving a request is, with ``APPROVE_ACTION`` on."""
SUBMIT_PURPOSE: Final = "approvals.submit"
"""The action submitting a request is, with ``SUBMIT_ACTION`` on."""
REJECT_PURPOSE: Final = "approvals.reject"
"""The action rejecting a request is, with ``REJECT_ACTION`` on."""
CANCEL_PURPOSE: Final = "approvals.cancel"
"""The action cancelling a request is, with ``CANCEL_ACTION`` on."""
TITLE: Final = gettext_noop("Approve a request")
"""What the approver's screen says above the request."""
SUBMIT_TITLE: Final = gettext_noop("Submit a request")
"""What the maker's screen says above the command they submit."""
REJECT_TITLE: Final = gettext_noop("Reject a request")
"""What the approver's screen says above the request they reject."""
CANCEL_TITLE: Final = gettext_noop("Cancel a request")
"""What the maker's screen says above the request they withdraw."""
ACTION_LABEL: Final = gettext_noop("Action")
"""The label of the request's action, the first value shown."""
REASON_LABEL: Final = gettext_noop("Reason")
"""The label of the reason of a rejection or a cancellation, shown second."""


def subject_of(command: ApproveRequest | RejectRequest | CancelRequest) -> str:
    """What the decision is on: ``approvals.request:<id>``."""
    return f"approvals.request:{command.request_id}"


def submitted_on(command: SubmitRequest) -> str:
    """What a submission is on: ``approvals.action:<action>``."""
    return f"approvals.action:{command.action}"


async def show(command: ApproveRequest) -> Shown:
    """The request's action and arguments, from the stored request.

    Asking is refused as approving would be: a request no longer pending,
    the maker, a party to the command, someone who decided it already, and
    a fingerprint that is not the request's. With
    ``AUTH.AUTHORIZATIONS.WHOLE`` on, a request that cannot be shown whole
    is refused too (``TooMuchToShow``).
    """
    request = await check_approval(command)
    details = details_of(request.action, await _shown(request), whole=whole())
    return Shown(fitted(gettext(TITLE)), details)


async def show_submission(command: SubmitRequest) -> Shown:
    """The action and the arguments it would be stored with, maker included.

    Asking is refused as submitting would be: an unknown action, arguments
    that do not make its command, a command no policy holds; and with
    ``AUTH.AUTHORIZATIONS.WHOLE`` on, one that cannot be shown whole.
    """
    arguments = check_submission(command)
    guard = guard_for(command.action)
    shown = await shown_of(guard, guard.load(arguments))
    details = details_of(command.action, shown, whole=whole())
    return Shown(fitted(gettext(SUBMIT_TITLE)), details)


async def show_rejection(command: RejectRequest) -> Shown:
    """The request's action, the reason, and the arguments, from the stored request.

    Asking is refused as rejecting would be: a request no longer pending,
    the maker, a party to the command, someone who decided it already.
    Never for what the request shows: stopping a request does nothing it
    does not show, so what does not fit is cut and counted, and what its
    maker typed is not checked. The reason given is the approver's own
    (since 2.8): ``Typed``, and with ``AUTH.AUTHORIZATIONS.WHOLE`` on,
    refused past a line (``TooMuchToShow``), as an impersonation's is.
    """
    request = await check_rejection(command)
    reason = stated(command.reason)
    shown = await _shown(request)
    details = details_of(request.action, shown, reason=reason, typed=False)
    return Shown(fitted(gettext(REJECT_TITLE)), details)


async def show_cancellation(command: CancelRequest) -> Shown:
    """The request's action, the reason if any, and the arguments, stored.

    Asking is refused as cancelling would be: a request no longer pending,
    or someone other than its maker. Never for what the request shows, as
    for a rejection; the reason given is checked as a rejection's is.
    """
    request = await check_cancellation(command)
    reason = stated(command.reason)
    shown = await _shown(request)
    details = details_of(request.action, shown, reason=reason, typed=False)
    return Shown(fitted(gettext(CANCEL_TITLE)), details)


def stated(reason: str) -> Typed:
    """A rejection's or a cancellation's ``reason``, as its screen shows it: ``Typed``.

    Raises:
        TooMuchToShow: With ``AUTH.AUTHORIZATIONS.WHOLE`` on, it is longer
            than a line (since 2.8).
    """
    if reason and whole():
        laid_out([(gettext(REASON_LABEL), reason)], whole=True)
    return Typed(reason)


async def check_approvable(guard: Guard, command: object) -> None:
    """Refuse a command its approvers could not be shown whole, if they must be.

    ``submit`` asks it with ``APPROVE_ACTION`` on: under
    ``AUTH.AUTHORIZATIONS.WHOLE``, a request no approver's screen shows
    whole could never be approved, so it is never stored.

    It is refused too where what it typed would be refused on that
    screen: with ``TYPED_LINKS`` refused, or an ``on_typed_text`` check
    registered (since 2.8).

    Raises:
        TooMuchToShow: ``WHOLE`` is on, and approving it would not fit.
        LinkNotShown: ``TYPED_LINKS`` is refused, and what approving it
            shows typed holds a link.
    """
    if whole() or typed_checked():
        details_of(guard.action, await shown_of(guard, command), whole=whole())


def whole() -> bool:
    """Whether acts show what they do whole, or are refused (auth's ``WHOLE``)."""
    return registry.settings(AuthSettings).AUTHORIZATIONS.WHOLE


def typed_checked() -> bool:
    """Whether a screen refuses some typed text: auth's ``TYPED_LINKS``, or a check."""
    refused = registry.settings(AuthSettings).AUTHORIZATIONS.TYPED_LINKS == "refused"
    return refused or any(True for _ in on_typed_text)


async def _shown(request: ApprovalRequest) -> Iterable[tuple[str, object]]:
    """What the request shows: its guard's ``show``, or its arguments by name.

    Raises:
        PolicyRemoved: Its guard or policy no longer exists.
    """
    guard, _ = policy_of(request)
    return await shown_of(guard, guard.load(request.arguments))


async def shown_of(guard: Guard, command: object) -> Iterable[tuple[str, object]]:
    """What ``command`` shows: ``guard.show``'s values, or its arguments in order.

    An argument that is ``Money``, or a list's item that is, shows as its
    amount and currency (``1500.00 USD``), not as its JSON.
    """
    shown = await guard.shown(command)
    if shown is not None:
        return shown
    return [
        (name, _readable(getattr(command, name, None), value))
        for name, value in guard.dump(command).items()
    ]


def _readable(value: object, dumped: object) -> object:
    """``dumped``, the JSON of ``value``, with each ``Money`` of it kept as itself."""
    if isinstance(value, Money):
        return value
    if isinstance(value, list | tuple) and isinstance(dumped, list):
        return [
            each if isinstance(each, Money) else item
            for each, item in zip(value, dumped, strict=True)
        ]
    return dumped


def details_of(
    action: str,
    arguments: Mapping[str, Any] | Iterable[tuple[str, Any]],
    *,
    reason: str = "",
    whole: bool = False,
    typed: bool = True,
) -> list[tuple[str, str]]:
    """The action, the reason if any, then the arguments by name, on a screen.

    Each value is a line: a string as itself, anything else as compact
    JSON; a list goes over as many lines as it takes ("added", "added
    (2)"), item by item, each item a string as itself or compact JSON, as
    ``laid_out`` lays it out. A value JSON has not (a ``Decimal``, a
    ``UUID``, a date a guard's ``show`` gives) shows as its text. With
    ``whole``, what does not fit whole is refused (``TooMuchToShow``);
    otherwise a value is cut to 64 characters, a list's last line says how
    many more, and with more than fit, a last line how many arguments.
    A ``Typed`` reason or argument is checked as ``laid_out`` checks one;
    without ``typed``, an argument's mark is dropped, unchecked.
    """
    pairs = arguments.items() if isinstance(arguments, Mapping) else arguments
    details: list[tuple[str, object]] = [(gettext(ACTION_LABEL), action)]
    if reason:
        details.append((gettext(REASON_LABEL), reason))
    details.extend((str(name), _jsonable(value, typed)) for name, value in pairs)
    return laid_out(details, whole=whole, more=_more_arguments)


def _jsonable(value: object, typed: bool = True) -> object:
    """``value`` as JSON: what JSON has as itself, anything else as its text.

    A ``Typed`` text stays marked, at the top or in a list, unless not ``typed``.
    """
    if isinstance(value, str):
        return value if typed or type(value) is str else str(value)
    if value is None or isinstance(value, int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(each) for key, each in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(each, typed) for each in value]
    return str(value)


def _more_arguments(hidden: int) -> str:
    """The last line's words: how many arguments are not shown."""
    more = ngettext("{count} more argument", "{count} more arguments", hidden)
    return more.format(count=hidden)


APPROVE: Final = Action(
    PURPOSE,
    command=ApproveRequest,
    subject=subject_of,
    show=show,
)
"""Approving one request, holding the fingerprint the approver was shown."""
SUBMIT: Final = Action(
    SUBMIT_PURPOSE,
    command=SubmitRequest,
    subject=submitted_on,
    show=show_submission,
)
"""Submitting one command, with its arguments, at ``POST /requests``."""
REJECT: Final = Action(
    REJECT_PURPOSE,
    command=RejectRequest,
    subject=subject_of,
    show=show_rejection,
)
"""Rejecting one request, for the reason the approver gives."""
CANCEL: Final = Action(
    CANCEL_PURPOSE,
    command=CancelRequest,
    subject=subject_of,
    show=show_cancellation,
)
"""Withdrawing one request, for the reason its maker gives."""
ACTIONS: Final = {
    "SUBMIT_ACTION": SUBMIT,
    "APPROVE_ACTION": APPROVE,
    "REJECT_ACTION": REJECT,
    "CANCEL_ACTION": CANCEL,
}
"""Each action, by the setting that turns it on."""

approve_authorized = requires_authorization(APPROVE)(record_approval)
"""``record_approval``, spending the request's authorization of the approval."""
submit_authorized = requires_authorization(SUBMIT)(record_submission)
"""``record_submission``, spending the request's authorization of the submission."""
reject_authorized = requires_authorization(REJECT)(record_rejection)
"""``record_rejection``, spending the request's authorization of the rejection."""
cancel_authorized = requires_authorization(CANCEL)(record_cancellation)
"""``record_cancellation``, spending the authorization of the cancellation."""


def register(config: ApprovalsSettings) -> None:
    """Register with auth each action ``config`` turns on (in ``ready``)."""
    for setting, action in ACTIONS.items():
        if getattr(config, setting):
            register_action(action)


async def ask(
    request: HTTPConnection,
    setting: str,
    command: object,
    *,
    uow: object = None,
    if_match: str | None = None,
) -> dict[str, Any]:
    """Store an authorization of the act ``command`` makes; what the client sees.

    The act is the action ``setting`` turns on. It is refused as
    ``request_action_authorization`` refuses, and as the action's ``show``
    does. ``uow``, the twin's, held to ``if_match``, binds the act to the
    version the page read (ADR 0178).
    """
    asked = await request_action_authorization(
        request,
        ACTIONS[setting],
        command,
        unit_of_work=None if uow is None else lambda: cast(UnitOfWork, uow),
        if_match=if_match,
    )
    return ActionAuthorizationOut.of(asked).model_dump()
