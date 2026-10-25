"""Who runs a command that changes or erases data, and the record of it.

Commands that act on stored data as an operator, not as the application
(``messages --retry``, ``auditforget``, ``loaddata``, ``flush``,
``partitions --prune``, ``privacy``), say who runs them: ``--operator``,
else ``DOMAINKIT_OPERATOR``, else the user the process runs as. The command
runs as the actor ``operator:<name>``, so what it writes to the audit trail
names them, and it records each action it takes there
(``operator.<command>``) with its outcome, when ``domainkit.contrib.audit``
is installed.
"""

import getpass
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from domainkit.apps import registry
from domainkit.context import RequestContext, bind_request

from .base import CommandError

OPERATOR_VARIABLE: Final = "DOMAINKIT_OPERATOR"
"""The environment variable naming the operator when ``--operator`` does not."""
OPERATOR: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}")
"""An operator's name: up to 64 letters, digits, ``.``, ``_``, ``@``, ``+``, ``-``."""
AUDIT: Final = "domainkit.contrib.audit"
ACTOR_PREFIX: Final = "operator:"

type Source = Literal["option", "environment", "system"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Operator:
    """Who runs a command, and where the name came from."""

    name: str
    source: Source
    """``option`` (``--operator``), ``environment`` (``DOMAINKIT_OPERATOR``),
    or ``system`` (the user the process runs as: the weakest)."""

    @property
    def actor(self) -> str:
        """The actor the command runs as: ``operator:<name>``."""
        return f"{ACTOR_PREFIX}{self.name}"


def operator(option: str | None = None) -> Operator:
    """The operator: ``option``, ``DOMAINKIT_OPERATOR``, or the process's user.

    Raises:
        CommandError: The name is not one ``OPERATOR`` allows, or none is
            found. The message never quotes the name.
    """
    if option is not None:
        return _checked(option, "option", "--operator")
    if from_environment := os.environ.get(OPERATOR_VARIABLE):
        return _checked(from_environment, "environment", OPERATOR_VARIABLE)
    try:
        user = getpass.getuser()
    except OSError:  # No name for the process's user id (a bare container).
        user = ""
    if not user:
        raise CommandError(
            f"Name the operator who runs this command: --operator NAME, or "
            f"{OPERATOR_VARIABLE}."
        )
    return _checked(user, "system", "the system user name")


def _checked(name: str, source: Source, where: str) -> Operator:
    """``name`` from ``source`` as an operator, if it is one ``OPERATOR`` allows."""
    if not OPERATOR.fullmatch(name):
        raise CommandError(
            f"The operator from {where} must be 1 to 64 letters, digits, '.', "
            "'_', '@', '+', or '-', starting with a letter or digit; give "
            "--operator NAME."
        )
    return Operator(name, source)


@contextmanager
def acting_as(who: Operator, command: str) -> Iterator[None]:
    """Run the block as ``who``: what it audits names them as its actor."""
    context = RequestContext(
        request_id=uuid.uuid4().hex,
        client_ip=None,
        method="COMMAND",
        path=command,
        actor=who.actor,
    )
    with bind_request(context):
        yield


@asynccontextmanager
async def recorded(
    who: Operator, action: str, **details: object
) -> AsyncIterator[dict[str, object]]:
    """Record ``operator.<action>`` once the block ends, with its outcome.

    The block may add details (counts) to the mapping it is given. A block
    that raises is recorded as a failure, with the error's class only, and
    the error goes on. Nothing is recorded without the audit app.
    """
    found: dict[str, object] = dict(details)
    try:
        yield found
    except Exception as exc:
        await _record(who, action, "failure", {**found, "error": type(exc).__name__})
        raise
    await _record(who, action, "success", found)


async def _record(
    who: Operator, action: str, outcome: str, details: dict[str, object]
) -> None:
    """Write the audit row, unless the audit app is not installed.

    A failure to write it after the action failed is logged, so the
    action's own error is the one raised.
    """
    if not registry.is_installed(AUDIT):
        return
    from domainkit.contrib.audit import Outcome, record

    try:
        await record(
            f"operator.{action}",
            Outcome(outcome),
            operator_source=who.source,
            **cast(dict[str, Any], details),  # Never target, personal, subject.
        )
    except Exception as exc:
        if outcome == "success":
            raise
        logger.error(  # Not the error itself: it could quote the details.
            "Could not record the failed operator action %s (%s)",
            action,
            type(exc).__name__,
        )
