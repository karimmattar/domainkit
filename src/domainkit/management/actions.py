"""Commands of several actions: ``manage.py <command> <action> [options]``.

Every command that does more than one thing has this one shape: each action
a method, its options declared beside it, its help in ``--help``::

    class Command(ActionCommand):
        help = "Manage the data keys of KEYS."

        @action(help="make a data key", options=[option("--id", dest="key_id")])
        async def generate(self, *, key_id: str | None) -> None: ...

        @action(help="count what each key seals")
        async def status(self) -> None: ...

``manage.py keys generate --id 2026-10`` runs ``generate(key_id="2026-10")``;
``common_options`` are taken by every action, as a ``--database``;
``manage.py keys --help`` lists the actions, ``manage.py keys status --help``
one's options. ``name=`` names an action other than its method (a name with
a ``-``, or a Python keyword). Options are ``ArgumentParser.add_argument``'s,
positional ones included; a command with ``operator_actions`` takes
``--operator`` after any action.
"""

from argparse import ArgumentParser
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from domainkit.management.base import BaseCommand

__all__ = ["ActionCommand", "Option", "action", "option"]

_ACTION: Final = "__domainkit_action__"
"""Where an action method keeps how it is declared."""


@dataclass(frozen=True, slots=True)
class Option:
    """One option of an action: ``ArgumentParser.add_argument``'s arguments."""

    names: tuple[str, ...]
    settings: dict[str, Any] = field(default_factory=dict)


def option(*names: str, **settings: Any) -> Option:  # noqa: ANN401 - add_argument's.
    """An option of an action, as ``parser.add_argument(*names, **settings)`` takes it.

    Raises:
        TypeError: No name is given.
    """
    if not names:
        raise TypeError("option() needs a name, such as '--batch-size' or 'subject'.")
    return Option(names, settings)


@dataclass(frozen=True, slots=True)
class _Declared:
    """How an action is declared: its name, help, options, and whether it operates."""

    name: str
    help: str
    options: tuple[Option, ...]
    operator: bool


type Handler = Callable[..., Awaitable[None] | None]


class _Declare(Protocol):
    """``@action(...)``: gives back the method it marks, its type unchanged."""

    def __call__[MethodT: Callable[..., object]](self, method: MethodT, /) -> MethodT:
        """Mark ``method`` as an action."""
        ...


def action(
    *,
    name: str | None = None,
    help: str,
    options: Sequence[Option] = (),
    operator: bool = False,
) -> _Declare:
    """Declare a method of an ``ActionCommand`` as one of its actions.

    ``name`` defaults to the method's, with ``_`` as ``-``; ``help`` is the
    line ``--help`` shows; ``options`` are the action's own (``option``).
    ``operator`` marks an action that changes or erases stored data: it
    takes ``--operator`` and runs as that operator (``operator_actions``
    marks every action of the command).
    """

    def declare[MethodT: Callable[..., object]](method: MethodT) -> MethodT:
        setattr(
            method,
            _ACTION,
            _Declared(
                name or method.__name__.replace("_", "-"),
                help,
                tuple(options),
                operator,
            ),
        )
        return method

    return declare


class ActionCommand(BaseCommand):
    """A command whose first argument picks one of its ``@action`` methods."""

    common_options: Sequence[Option] = ()
    """Options every action takes, after its own (as ``--database``)."""
    default_action: str | None = None
    """The action the command runs when none is named (as ``flags``: ``list``);
    None makes naming one required."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare one sub-parser per action, with its options.

        Raises:
            TypeError: The class declares no action, or two with one name.
        """
        actions = parser.add_subparsers(
            dest="action", required=self.default_action is None
        )
        self._parsers: dict[str, ArgumentParser] = {}
        for declared in self.declared_actions().values():
            sub = actions.add_parser(
                declared.name, help=declared.help, description=declared.help
            )
            self._parsers[declared.name] = sub
            for each in (*declared.options, *self.common_options):
                sub.add_argument(*each.names, **each.settings)
            if self._operates(declared):
                super().add_operator_argument(sub)

    def add_operator_argument(self, parser: ArgumentParser) -> None:
        """Nothing: each action that operates takes ``--operator`` itself."""

    def execute(self, **options: object) -> None:
        """Run the action, as its operator if it is one that operates."""
        if options.get("action") is None and self.default_action is not None:
            defaults = {
                each.dest: each.default
                for each in self._parsers[self.default_action]._actions
                if each.dest != "help"
            }
            options = {**defaults, **options, "action": self.default_action}
        declared = self.declared_actions().get(str(options.get("action")))
        if declared is not None and self._operates(declared):
            self.operator_actions = True
        super().execute(**options)

    def _operates(self, declared: _Declared) -> bool:
        """Whether ``declared`` runs as an operator: its own mark, or the class's."""
        return declared.operator or type(self).operator_actions

    def label(self, options: dict[str, object]) -> str:
        """What the audit trail calls this run: the command and its action."""
        return f"{super().label(options)} {options['action']}"

    @classmethod
    def declared_actions(cls) -> dict[str, _Declared]:
        """The class's actions by name, in the order they are declared.

        Raises:
            TypeError: The class declares none, or two with one name.
        """
        found: dict[str, _Declared] = {}
        for attribute in _methods(cls):
            declared: _Declared | None = getattr(attribute, _ACTION, None)
            if declared is None:
                continue
            if declared.name in found:
                raise TypeError(
                    f"{cls.__name__} declares the action {declared.name!r} twice."
                )
            found[declared.name] = declared
        if not found:
            raise TypeError(f"{cls.__name__} declares no @action.")
        return found

    def handle(self, *, action: str, **options: Any) -> Awaitable[None] | None:  # noqa: ANN401 - the action's options.
        """Run the method the ``action`` names, with its options."""
        for attribute in _methods(type(self)):
            declared: _Declared | None = getattr(attribute, _ACTION, None)
            if declared is not None and declared.name == action:
                method: Handler = getattr(self, attribute.__name__)
                return method(**options)
        raise LookupError(f"{type(self).__name__} has no action {action!r}.")


def _methods(cls: type) -> list[Any]:
    """The functions ``cls`` and its bases define, the bases' first, overrides once."""
    seen: dict[str, Any] = {
        name: attribute
        for klass in reversed(cls.__mro__)
        for name, attribute in vars(klass).items()
        if callable(attribute)
    }
    return list(seen.values())
