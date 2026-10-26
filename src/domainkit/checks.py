"""System checks: what ``check`` and startup find, each with an id and a level.

Every problem the framework reports about a configuration is a
``CheckMessage``: a string, the message itself, that also carries an id
such as ``security.E001`` and, where the message does not say how to fix
it, a ``hint``. The letter in the id is the level: ``E`` for an error,
``W`` for a warning, ``I`` for information.

- Errors refuse: settings that fail the deployment baseline do not load,
  and staging and production refuse an application with an error. Every
  error found is reported at once, not one per run.
- Warnings are listed by ``check``; ``check --deploy`` fails on them, so a
  deployment pipeline can gate on them.
- A project silences a warning it has judged by listing its id in
  ``SILENCED_CHECKS``. Errors cannot be silenced: each one stands for an
  unsafe or broken configuration, and deny-by-default keeps it refused
  until the configuration changes.

Messages are plain strings to code that expects them, so the hooks that
return ``list[str]`` (``DomainConfig.check_application`` and
``deployment_warnings``) may return ``CheckMessage`` values; a plain string
is reported as it is, without an id, and cannot be silenced.
"""

import re
from collections.abc import Iterable, Sequence
from enum import IntEnum
from typing import Final, Self

from domainkit.exceptions import ImproperlyConfigured

__all__ = [
    "MAX_SILENCED_CHECKS",
    "CheckFailed",
    "CheckMessage",
    "Level",
    "describe",
    "silence",
    "validate_silenced",
]

ID: Final = re.compile(r"[a-z][a-z0-9_]{0,31}\.([EWI])[0-9]{3}")
"""A check id: a namespace, a dot, the level's letter, and three digits."""
MAX_SILENCED_CHECKS: Final = 64
"""The most ids ``SILENCED_CHECKS`` may list."""


class Level(IntEnum):
    """How serious a check's finding is, read from its id's letter."""

    INFO = 20
    WARNING = 30
    ERROR = 40


_LEVELS: Final = {"E": Level.ERROR, "W": Level.WARNING, "I": Level.INFO}


class CheckMessage(str):
    """One finding of a check: the message, with an ``id`` and a ``hint``.

    It is the message as a string, equal to it, so code that collects
    problems as strings keeps working; ``describe`` shows the id and the
    hint too.
    """

    id: str
    """The check's id, such as ``security.W001``."""
    hint: str
    """How to fix it, where the message does not say."""

    def __new__(cls, id: str, message: str, hint: str = "") -> Self:
        """Make the finding ``id`` with ``message``, and how to fix it (``hint``).

        Raises:
            ValueError: ``id`` is not a check id, such as ``security.W001``.
        """
        if not ID.fullmatch(id):
            raise ValueError(f"{id!r} is not a check id, such as 'security.W001'.")
        found = super().__new__(cls, message)
        found.id = id
        found.hint = hint
        return found

    @property
    def level(self) -> Level:
        """``ERROR``, ``WARNING``, or ``INFO``, from the id's letter."""
        return _LEVELS[self.id.partition(".")[2][0]]

    @property
    def message(self) -> str:
        """The message, as a plain string."""
        return str.__str__(self)

    def __repr__(self) -> str:
        """Show the id and the message."""
        return f"<CheckMessage {self.id}: {self.message!r}>"

    def __reduce__(self) -> tuple[type[Self], tuple[str, str, str]]:
        """Pickle and copy with the id and the hint."""
        return type(self), (self.id, self.message, self.hint)


class CheckFailed(ImproperlyConfigured):
    """Checks found errors; the message lists every one, with its id."""

    def __init__(self, heading: str, messages: Sequence[str]) -> None:
        """Report ``messages`` under ``heading``, one per line."""
        super().__init__(f"{heading}:\n" + "\n".join(describe(m) for m in messages))
        self.messages = list(messages)


def describe(message: str, indent: str = "  ", label: str = "") -> str:
    """One finding as ``check`` prints it: the id, the message, the hint.

    ``label`` goes before the id, as ``Warning: ``.
    """
    if not isinstance(message, CheckMessage):
        return f"{indent}{label}{message}"
    line = f"{indent}{label}{message.id}: {message.message}"
    if message.hint:
        line += f"\n{indent}  Hint: {message.hint}"
    return line


def silence[S: str](messages: Iterable[S], silenced: Iterable[str]) -> list[S]:
    """``messages`` without those whose id ``SILENCED_CHECKS`` lists.

    Errors are never silenced, whatever the list says (``validate_silenced``
    refuses to list one).
    """
    ids = frozenset(silenced)
    return [
        message
        for message in messages
        if not (
            isinstance(message, CheckMessage)
            and message.id in ids
            and message.level < Level.ERROR
        )
    ]


def validate_silenced(ids: Sequence[str]) -> tuple[str, ...]:
    """Check ``SILENCED_CHECKS``: ids of warnings, each once, and not too many.

    Raises:
        ValueError: An entry is not a check id, names an error, is listed
            twice, or there are more than ``MAX_SILENCED_CHECKS``.
    """
    if len(ids) > MAX_SILENCED_CHECKS:
        raise ValueError(
            f"SILENCED_CHECKS lists {len(ids)} ids; at most {MAX_SILENCED_CHECKS}."
        )
    if len(set(ids)) != len(ids):
        raise ValueError("SILENCED_CHECKS lists an id twice")
    for entry in ids:
        match = ID.fullmatch(entry)
        if match is None:
            raise ValueError(
                f"SILENCED_CHECKS: {entry!r} is not a check id, such as 'security.W001'"
            )
        if match[1] == "E":
            raise ValueError(
                f"SILENCED_CHECKS cannot silence the error {entry}: an error "
                "stands for an unsafe or broken configuration; change the "
                "configuration instead"
            )
    return tuple(ids)
