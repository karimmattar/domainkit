"""Who runs a command that changes or erases data, and the record of it.

Commands that act on stored data or credentials as an operator, not as
the application (``messages --retry``, ``auditforget``, ``loaddata``,
``flush``, ``partitions --prune``, ``privacy``, ``resetmfa``,
``createtoken``, ``flags``, ``webhooks replay``, ...), say who runs them:
``--operator``, else ``DOMAINKIT_OPERATOR``, else the login name
``getpass.getuser()`` reports (``LOGNAME`` or ``USER`` first, then the
process's user id's name), which the caller can set as easily. A process
whose user has no name, or none ``OPERATOR`` allows (a container run under
an arbitrary uid, from cron), runs as ``uid-<n>``, its user id, so commands
that ran unattended keep running; the source says ``system``. The command
runs as the actor ``operator:<name>``, so what it writes to the audit trail
names them, and it records each action it takes there
(``operator.<command>``) with its outcome, when an installed domain keeps
such records (``OperatorRecorder``: ``domainkit.contrib.audit``).
"""

import getpass
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.context import RequestContext, bind_request

from .base import CommandError

OPERATOR_VARIABLE: Final = "DOMAINKIT_OPERATOR"
"""The environment variable naming the operator when ``--operator`` does not."""
OPERATOR: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}")
"""An operator's name: up to 64 letters, digits, ``.``, ``_``, ``@``, ``+``, ``-``."""
UNNAMED: Final = "unnamed"
"""The operator of a process with no user name nor user id (no ``os.getuid``)."""
ACTOR_PREFIX: Final = "operator:"

type Source = Literal["option", "environment", "system"]

logger = logging.getLogger(__name__)


@runtime_checkable
class OperatorRecorder(Protocol):
    """An installed domain that keeps the record of operators' actions.

    ``domainkit.contrib.audit`` is one: it writes ``operator.<action>`` to
    the audit trail. The details never hold a target, personal data, or a
    subject; a failure to record a successful action fails the command.
    """

    async def record_operator_action(
        self, action: str, outcome: str, details: Mapping[str, object]
    ) -> None:
        """Record that the operator took ``action`` (``success``, ``failure``)."""
        ...


@dataclass(frozen=True, slots=True)
class Operator:
    """Who runs a command, and where the name came from."""

    name: str
    source: Source
    """``option`` (``--operator``), ``environment`` (``DOMAINKIT_OPERATOR``),
    or ``system`` (the login name ``getpass.getuser()`` reports, from the
    environment first, or else ``uid-<n>``: the weakest)."""

    @property
    def actor(self) -> str:
        """The actor the command runs as: ``operator:<name>``."""
        return f"{ACTOR_PREFIX}{self.name}"


def operator(option: str | None = None) -> Operator:
    """The operator: ``option``, ``DOMAINKIT_OPERATOR``, or the process's user.

    The process's user is its login name, if ``OPERATOR`` allows it, else
    ``uid-<n>`` (``unnamed`` where there are no user ids): a container
    under an arbitrary uid has no name, and a Windows domain account or a
    name with a space is none ``OPERATOR`` allows. Such a name is never
    kept or shown.

    Raises:
        CommandError: The name given with ``--operator`` or
            ``DOMAINKIT_OPERATOR`` is not one ``OPERATOR`` allows. The
            message never quotes the name.
    """
    if option is not None:
        return _checked(option, "option", "--operator")
    if from_environment := os.environ.get(OPERATOR_VARIABLE):
        return _checked(from_environment, "environment", OPERATOR_VARIABLE)
    try:
        user = getpass.getuser()
    except OSError:  # No name for the process's user id (a bare container).
        user = ""
    if OPERATOR.fullmatch(user):
        return Operator(user, "system")
    return Operator(_process_user_id(), "system")


def _process_user_id() -> str:
    """``uid-<n>`` of the process, or ``unnamed`` without user ids (Windows)."""
    getuid = getattr(os, "getuid", None)
    return UNNAMED if getuid is None else f"uid-{getuid()}"


def _checked(name: str, source: Source, where: str) -> Operator:
    """``name`` from ``source`` as an operator, if it is one ``OPERATOR`` allows."""
    if not OPERATOR.fullmatch(name):
        raise CommandError(
            f"The operator from {where} must be 1 to 64 letters, digits, '.', "
            "'_', '@', '+', or '-', starting with a letter or digit; give "
            "--operator NAME."
        )
    return Operator(name, source)


def require_named(who: Operator | None, command: str) -> None:
    """Refuse an operator nobody named, in staging and production.

    ``loaddata`` and ``flush`` there need ``--operator`` or
    ``DOMAINKIT_OPERATOR``: the login name, or ``uid-<n>``, is what the
    process happens to run as, not who chose to write or delete rows.

    Raises:
        CommandError: ``ENVIRONMENT`` is deployed and ``who`` is the
            process's user (source ``system``), or nobody.
    """
    if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS and (
        who is None or who.source == "system"
    ):
        raise CommandError(
            f"Name the operator who runs {command} in {settings.ENVIRONMENT}: "
            f"give --operator NAME, or set {OPERATOR_VARIABLE}."
        )


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
    """Write the audit row, unless no installed domain keeps one (``OperatorRecorder``).

    A failure to write it after the action failed is logged, so the
    action's own error is the one raised.
    """
    recorders = [config for config in registry if isinstance(config, OperatorRecorder)]
    if not recorders:
        return
    try:
        for recorder in recorders:
            await recorder.record_operator_action(
                action, outcome, {"operator_source": who.source, **details}
            )
    except Exception as exc:
        if outcome == "success":
            raise
        logger.error(  # Not the error itself: it could quote the details.
            "Could not record the failed operator action %s (%s)",
            action,
            type(exc).__name__,
        )
