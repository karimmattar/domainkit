"""``ActionCommand``: one shape for every command of several actions."""

import io

import pytest

from domainkit.context import current_request
from domainkit.management import ActionCommand, action, option


class Tool(ActionCommand):
    """Two actions, one with options, one named apart from its method."""

    help = "A tool."
    requires_setup = False

    def __init__(self) -> None:
        """Write to a buffer, and remember what ran."""
        super().__init__(stdout=io.StringIO())
        self.ran: list[tuple[str, dict[str, object]]] = []

    @action(
        help="count things",
        options=[
            option("subject", help="what to count"),
            option("--limit", type=int, default=10, help="at most this many"),
        ],
    )
    def count(self, *, subject: str, limit: int) -> None:
        """Remember the call."""
        self.ran.append(("count", {"subject": subject, "limit": limit}))

    @action(name="clear-all", help="clear everything")
    async def clear_all(self) -> None:
        """Remember the call, from a coroutine."""
        self.ran.append(("clear-all", {}))

    def helper(self) -> str:
        """An ordinary method, not an action."""
        return "not an action"


class Audited(Tool):
    """A subclass that acts as an operator and replaces one action."""

    operator_actions = True

    @action(help="count things, audited", options=[option("subject")])
    def count(self, *, subject: str) -> None:  # type: ignore[override]
        """Remember the call and the operator."""
        assert self.operator is not None
        self.ran.append(("audited", {"subject": subject, "by": self.operator.name}))


def run(command: ActionCommand, *argv: str) -> None:
    """Parse ``argv`` as ``manage.py tool ...`` does, and execute."""
    command.run_from_argv(["manage.py", "tool", *argv])


def test_the_first_argument_picks_the_action_and_its_options() -> None:
    """Positional and optional options reach the method by name."""
    tool = Tool()

    run(tool, "count", "apples", "--limit", "3")
    run(tool, "clear-all")

    assert tool.ran == [("count", {"subject": "apples", "limit": 3}), ("clear-all", {})]


def test_help_lists_the_actions_and_each_its_own_options() -> None:
    """``--help`` names every action; an action's names its options."""
    parser = Tool().create_parser("manage.py", "tool")
    listing = " ".join(parser.format_help().split())
    sub = parser._subparsers._group_actions[0].choices["count"]  # type: ignore[union-attr, index]

    assert "{count,clear-all}" in listing
    assert "count count things" in listing
    assert "clear-all clear everything" in listing
    assert "--limit LIMIT at most this many" in " ".join(sub.format_help().split())
    assert list(Tool.declared_actions()) == ["count", "clear-all"]


def test_an_action_is_required() -> None:
    """Without one, argparse refuses, as for any missing argument."""
    with pytest.raises(SystemExit):
        run(Tool())


def test_operators_name_themselves_after_any_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--operator`` is each action's own option; overrides replace actions."""
    monkeypatch.setenv("DOMAINKIT_OPERATOR", "fallback")
    audited = Audited()

    run(audited, "count", "pears", "--operator", "ann")

    assert audited.ran == [("audited", {"subject": "pears", "by": "ann"})]
    assert list(Audited.declared_actions()) == ["count", "clear-all"]


def test_declarations_that_cannot_work_are_refused() -> None:
    """No action, an action twice, an option without a name, an unknown action."""

    class Empty(ActionCommand):
        pass

    class Twice(ActionCommand):
        @action(name="go", help="one")
        def first(self) -> None:
            pass

        @action(name="go", help="two")
        def second(self) -> None:
            pass

    with pytest.raises(TypeError, match="Empty declares no @action"):
        Empty.declared_actions()
    with pytest.raises(TypeError, match="Twice declares the action 'go' twice"):
        Twice.declared_actions()
    with pytest.raises(TypeError, match="option\\(\\) needs a name"):
        option()
    with pytest.raises(LookupError, match="Tool has no action 'nope'"):
        Tool().handle(action="nope")
    assert Tool().helper() == "not an action"


class Mixed(ActionCommand):
    """One action that reads, one that changes data as an operator."""

    requires_setup = False

    def __init__(self) -> None:
        """Remember what ran, as whom, and under which label."""
        super().__init__(stdout=io.StringIO())
        self.ran: list[tuple[str, str | None, str]] = []

    @action(help="read")
    def show(self) -> None:
        """Remember the call."""
        self.ran.append(("show", None, ""))

    @action(help="erase", operator=True)
    def erase(self) -> None:
        """Remember the call, its operator, and the request's path."""
        assert self.operator is not None
        context = current_request()
        assert context is not None
        self.ran.append(("erase", self.operator.name, context.path))


def test_only_actions_that_operate_take_an_operator() -> None:
    """``operator=True`` is per action; the run is labelled with it."""
    parser = Mixed().create_parser("manage.py", "tool")
    choices = parser._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    mixed = Mixed()

    run(mixed, "show")
    run(mixed, "erase", "--operator", "ann")

    assert "--operator" not in choices["show"].format_help()  # type: ignore[index]
    assert "--operator NAME" in choices["erase"].format_help()  # type: ignore[index]
    assert mixed.ran == [
        ("show", None, ""),
        ("erase", "ann", "test_management_actions erase"),
    ]
