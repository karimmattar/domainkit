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

Only imported with one of them on, and so with ``domainkit.contrib.auth``
installed (``ApprovalsConfig.ready`` refuses it otherwise).
"""

import json
from collections.abc import Mapping
from typing import Any, Final

from starlette.requests import HTTPConnection

from domainkit.contrib.auth.actions import (
    Action,
    ActionAuthorizationOut,
    Shown,
    register_action,
    request_action_authorization,
    requires_authorization,
)
from domainkit.i18n import gettext, gettext_noop, ngettext

from .guards import policy_of
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
MORE_LABEL: Final = "…"
"""The label of the line that says how many arguments are not shown."""
MAX_DETAILS: Final = 6
"""The most labelled values an act shows (auth's ``Shown``)."""
MAX_TEXT: Final = 64
"""The longest title, label, and value an act shows (auth's ``Shown``)."""
CUT: Final = "…"
"""What ends a text cut to ``MAX_TEXT``."""


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
    a fingerprint that is not the request's.
    """
    request = await check_approval(command)
    return Shown(fitted(gettext(TITLE)), details_of(request.action, _shown(request)))


async def show_submission(command: SubmitRequest) -> Shown:
    """The action and the arguments it would be stored with, maker included.

    Asking is refused as submitting would be: an unknown action, arguments
    that do not make its command, a command no policy holds.
    """
    arguments = check_submission(command)
    return Shown(fitted(gettext(SUBMIT_TITLE)), details_of(command.action, arguments))


async def show_rejection(command: RejectRequest) -> Shown:
    """The request's action, the reason, and the arguments, from the stored request.

    Asking is refused as rejecting would be: a request no longer pending,
    the maker, a party to the command, someone who decided it already.
    """
    request = await check_rejection(command)
    details = details_of(request.action, _shown(request), reason=command.reason)
    return Shown(fitted(gettext(REJECT_TITLE)), details)


async def show_cancellation(command: CancelRequest) -> Shown:
    """The request's action, the reason if any, and the arguments, stored.

    Asking is refused as cancelling would be: a request no longer pending,
    or someone other than its maker.
    """
    request = await check_cancellation(command)
    details = details_of(request.action, _shown(request), reason=command.reason)
    return Shown(fitted(gettext(CANCEL_TITLE)), details)


def _shown(request: ApprovalRequest) -> Any:  # noqa: ANN401 - JSON.
    """The request's arguments, in its command's order.

    Raises:
        PolicyRemoved: Its guard or policy no longer exists.
    """
    guard, _ = policy_of(request)
    return guard.dump(guard.load(request.arguments))


def details_of(
    action: str, arguments: Mapping[str, Any], *, reason: str = ""
) -> list[tuple[str, str]]:
    """The action, the reason if any, then the arguments by name: six at most.

    Each value is compact JSON (a string as itself), made printable and cut
    to 64 characters. With more arguments than fit, all but one of the
    room left are shown, and a last line says how many more.
    """
    details = [(fitted(gettext(ACTION_LABEL)), fitted(action))]
    if reason:
        details.append((fitted(gettext(REASON_LABEL)), fitted(reason)))
    labels = {label for label, _ in details} | {MORE_LABEL}
    named = []
    for name, value in arguments.items():
        label = fitted(str(name))
        if label and label not in labels:
            labels.add(label)
            named.append((label, fitted(_text(value))))
    room = MAX_DETAILS - len(details)
    if len(named) == len(arguments) and len(named) <= room:
        return [*details, *named]
    kept = named[: room - 1]
    hidden = len(arguments) - len(kept)
    more = ngettext("{count} more argument", "{count} more arguments", hidden)
    return [*details, *kept, (MORE_LABEL, fitted(more.format(count=hidden)))]


def _text(value: object) -> str:
    """``value`` as text: a string as itself, anything else as compact JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def fitted(text: str) -> str:
    """``text`` as a screen shows it: printable, at most 64 characters.

    A character that is not printable is shown escaped, as Python writes
    it (a new line as a backslash and ``n``); a longer text is cut, and
    ends with ``…``.
    """
    printable = "".join(
        each if each.isprintable() else each.encode("unicode_escape").decode("ascii")
        for each in text
    )
    if len(printable) <= MAX_TEXT:
        return printable
    return printable[: MAX_TEXT - len(CUT)] + CUT


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


async def ask(request: HTTPConnection, setting: str, command: object) -> dict[str, Any]:
    """Store an authorization of the act ``command`` makes; what the client sees.

    The act is the action ``setting`` turns on. It is refused as
    ``request_action_authorization`` refuses, and as the action's ``show``
    does.
    """
    asked = await request_action_authorization(request, ACTIONS[setting], command)
    return ActionAuthorizationOut.of(asked).model_dump()
