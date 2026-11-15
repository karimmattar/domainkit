"""Approving with an authorization of its own: the action ``approvals.approve``.

With ``APPROVALS.APPROVE_ACTION`` on (ADR 0144), an approver confirms each
approval as one act: the request's id and the fingerprint they state are
the act's arguments, the request is its subject
(``approvals.request:<id>``), and the user is shown the request's action
and arguments, built here from the stored request. ``approve_request``
spends the authorization through ``approve_authorized``, with the
approval's commit where the requests share auth's database.

Only imported with the setting on, and so with ``domainkit.contrib.auth``
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
from domainkit.i18n import gettext, ngettext

from .guards import policy_of
from .services import ApproveRequest, check_approval, record_approval

PURPOSE: Final = "approvals.approve"
"""The action approving a request is, with ``APPROVE_ACTION`` on."""
TITLE: Final = "Approve a request"
"""What the approver's screen says above the request."""
ACTION_LABEL: Final = "Action"
"""The label of the request's action, the first value shown."""
MORE_LABEL: Final = "…"
"""The label of the line that says how many arguments are not shown."""
MAX_DETAILS: Final = 6
"""The most labelled values an act shows (auth's ``Shown``)."""
MAX_TEXT: Final = 64
"""The longest title, label, and value an act shows (auth's ``Shown``)."""
CUT: Final = "…"
"""What ends a text cut to ``MAX_TEXT``."""


def subject_of(command: ApproveRequest) -> str:
    """What the approval is on: ``approvals.request:<id>``."""
    return f"approvals.request:{command.request_id}"


async def show(command: ApproveRequest) -> Shown:
    """The request's action and arguments, from the stored request.

    Asking is refused as approving would be: a request no longer pending,
    the maker, a party to the command, someone who decided it already, and
    a fingerprint that is not the request's.
    """
    request = await check_approval(command)
    guard, _ = policy_of(request)
    arguments = guard.dump(guard.load(request.arguments))  # In the command's order.
    return Shown(fitted(gettext(TITLE)), details_of(request.action, arguments))


def details_of(action: str, arguments: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The action, then the arguments by name: six labelled values at most.

    Each value is compact JSON (a string as itself), made printable and cut
    to 64 characters. With more arguments than five, four are shown, and a
    last line says how many more.
    """
    details = [(fitted(gettext(ACTION_LABEL)), fitted(action))]
    labels = {details[0][0], MORE_LABEL}
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

approve_authorized = requires_authorization(APPROVE)(record_approval)
"""``record_approval``, spending the request's authorization of the approval."""


def register() -> None:
    """Register ``APPROVE`` with auth (``ApprovalsConfig.ready``)."""
    register_action(APPROVE)


async def ask(request: HTTPConnection, command: ApproveRequest) -> dict[str, Any]:
    """Store an authorization of approving as ``command`` says; what the client sees.

    Raises:
        See ``request_action_authorization``, and ``show``'s refusals.
    """
    asked = await request_action_authorization(request, APPROVE, command)
    return ActionAuthorizationOut.of(asked).model_dump()
