"""Mail the site's admins (Django's ``mail_admins``), and tell them of errors.

``MAIL["ADMINS"]`` names who hears. ``mail_admins`` writes to them like any
mail; ``report_to_admins`` is an ``ERROR_REPORTERS`` entry that mails them
each unexpected error::

    MAIL = {..., "ADMINS": ("ops@example.com",)}
    ERROR_REPORTERS = ("domainkit.contrib.mail.admins.report_to_admins",)

A report names the error's type, where it was raised, the frames of its
traceback, and the request's context (its id, path, and method): enough to
find the logs. It never holds the error's message, nor any value of the
frames, which may hold what a user sent. Each process mails at most one
report per type and place in ``ADMINS_EVERY``, so a failing dependency
does not flood the inbox; the mail goes straight through the backend,
from the reporting thread, not through the outbox.
"""

import asyncio
import threading
import traceback
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from domainkit import clock

from .delivery import deliver, mail_settings, send
from .message import EmailMessage

if TYPE_CHECKING:
    from domainkit.ddd import UnitOfWork

__all__ = ["mail_admins", "report_to_admins"]

MAX_FRAMES: Final = 30
"""Frames of a traceback a report lists, the innermost ones."""
MAX_REMEMBERED: Final = 1000
"""Errors whose last report each process remembers, to space the next."""
_lock = threading.Lock()
_last_sent: dict[tuple[str, str], float] = {}


async def mail_admins(
    subject: str, body: str, *, uow: "UnitOfWork | None" = None
) -> EmailMessage | None:
    """Queue a mail to ``MAIL["ADMINS"]``, with ``uow``'s commit or at once.

    None when no admin is named: nothing is sent.
    """
    admins = mail_settings().ADMINS
    if not admins:
        return None
    return await send(EmailMessage(subject=subject, body=body, to=admins), uow)


def report_to_admins(exc: BaseException, context: Mapping[str, object]) -> None:
    """Mail ``exc`` to the admins, unless one like it went out lately.

    An ``ERROR_REPORTERS`` entry: it runs on the reporting thread.
    """
    config = mail_settings()
    if not config.ADMINS:
        return
    frames = traceback.extract_tb(exc.__traceback__)[-MAX_FRAMES:]
    where = (
        f"{frames[-1].filename}:{frames[-1].lineno} in {frames[-1].name}"
        if frames
        else "an unknown place"
    )
    kind = f"{type(exc).__module__}.{type(exc).__qualname__}"
    if not _due((kind, where), config.ADMINS_EVERY.total_seconds()):
        return
    lines = [
        f"{kind} was raised at {where}.",
        "",
        "Context:",
        *(f"  {name}: {value}" for name, value in sorted(context.items())),
        "",
        "Traceback, innermost last (no values):",
        *(f"  {f.filename}:{f.lineno} in {f.name}" for f in frames),
        "",
        "The error's message is left out: it may hold what a user sent.",
        "Find it in the logs by the request id.",
    ]
    function = frames[-1].name if frames else "?"
    message = EmailMessage(
        subject=f"Error: {type(exc).__qualname__} in {function}",
        body="\n".join(lines),
        to=config.ADMINS,
    )
    asyncio.run(deliver([message]))


def _due(key: tuple[str, str], every: float) -> bool:
    """Whether a report of ``key`` may go now; if so, note that it went."""
    now = clock.monotonic()
    with _lock:
        last = _last_sent.get(key)
        if last is not None and now - last < every:
            return False
        if len(_last_sent) >= MAX_REMEMBERED:
            _last_sent.clear()
        _last_sent[key] = now
        return True
