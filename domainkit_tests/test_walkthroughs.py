"""The README's quick start and the tutorial work exactly as written.

Each test reads the pages' ``bash`` blocks and the Python blocks titled
with a file, and plays them in order in a new directory: every command as
a user would, in a subprocess of this interpreter; every edit as a reader
would make it (``walkthrough_edits.py``); and, after ``runserver``, every
``curl`` line against the project's application, served in process by
``walkthrough_driver.py`` at the address ``runserver`` would listen on,
until a line that is not one stops the server. Only the lines that set up
the environment (a virtual environment and ``pip install``) are left out,
and they must be the ones listed here, so a changed page fails until this
test knows what it says.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from domainkit_tests import walkthrough_edits

ROOT = Path(__file__).parents[1]
DRIVER = Path(__file__).with_name("walkthrough_driver.py")
TUTORIAL = ROOT / "docs" / "tutorial"
PARTS = (
    "1-first-resource.md",
    "2-rules.md",
    "3-permissions.md",
    "4-events.md",
    "5-testing-and-deploying.md",
)
"""The getting-started path, in the order a newcomer reads it."""
ENVIRONMENT_LINES = (
    "python3.13 -m venv .venv && . .venv/bin/activate",
    'pip install "domainkit[postgres,redis,sqlite,testing]"',
)
"""Lines that build the environment this interpreter already is."""
TYPED = {"PASSWORD": "correct-horse-battery", "CLERK_PASSWORD": "staple-lantern-oak"}
"""What the user types at each ``read -rsp`` prompt of the pages."""
QUIET = ("-p", "no:cacheprovider")
"""The generated project's tests leave no cache behind."""
_BLOCK = re.compile(r"^```bash\n(.*?)^```$", re.DOTALL | re.MULTILINE)
_FENCE = re.compile(
    r"^```(?P<info>[^\n]*)\n(?P<body>.*?)^```$", re.DOTALL | re.MULTILINE
)
_TITLE = re.compile(r'python title="(?P<path>[^"]+)"')
_SERVED = re.compile(r"curl |[A-Z_]+=\$\(|read -rsp |unset ")
"""Lines the driver runs while the server is up; any other line stops it."""


def shell_lines(markdown: str) -> list[str]:
    """The commands of every ``bash`` block, continuation lines joined."""
    lines: list[str] = []
    for block in _BLOCK.findall(markdown):
        lines += _joined(block)
    return lines


def _joined(block: str) -> list[str]:
    """A block's lines, continuation lines joined, blank lines left out."""
    joined = re.sub(r"\\\n\s*", "", block)
    return [line.strip() for line in joined.splitlines() if line.strip()]


def steps(markdown: str) -> list[tuple[str, str]]:
    """The page's commands and edits, in order: ``("$", line)`` or ``(path, code)``.

    Raises:
        AssertionError: A Python block names no file, so a reader could not
            tell where it goes.
    """
    found: list[tuple[str, str]] = []
    for match in _FENCE.finditer(markdown):
        info, body = match["info"].strip(), match["body"]
        if info == "bash":
            found += [("$", line) for line in _joined(body)]
        elif info.startswith("python"):
            title = _TITLE.fullmatch(info)
            assert title, f"a Python block without a file: {info!r}"
            found.append((title["path"], body))
    return found


def section(markdown: str, heading: str) -> str:
    """The text under ``## heading``, up to the next heading of that level."""
    _, _, rest = markdown.partition(f"\n## {heading}\n")
    assert rest, f"no '## {heading}' section"
    return rest.split("\n## ", 1)[0]


def words(line: str) -> list[str]:
    """The line's words as a shell splits them, without a trailing comment."""
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return list(lexer)


@dataclass
class Walk:
    """A shell's state: where it is, what it holds, and what it exports."""

    cwd: Path
    variables: dict[str, str] = field(default_factory=dict)
    """Every name the shell holds."""
    exported: set[str] = field(default_factory=set)
    """The names its processes inherit."""
    printed: dict[str, str] = field(default_factory=dict)
    """What each line printed, by line."""

    @property
    def env(self) -> dict[str, str]:
        """The environment a process started now gets."""
        inherited = {
            name: value
            for name, value in os.environ.items()
            # A user's shell sets none of these: the tests' settings, the
            # generated projects' own, or mutmut's.
            if not name.startswith(("DOMAINKIT_", "BILLING_API_", "BANK_API_"))
            and name != "MUTANT_UNDER_TEST"
        }
        return inherited | {name: self.variables[name] for name in self.exported}

    def run(self, line: str) -> None:
        """Run one line, as a shell would, and require it to succeed."""
        commands: list[list[str]] = [[]]
        for word in words(line):
            if word == "&&":
                commands.append([])
            else:
                commands[-1].append(word)
        for command in commands:
            command = [self.expand(word) for word in command]
            env = self.env
            while "=" in command[0]:
                name, _, value = command.pop(0).partition("=")
                env[name] = value
            output = None
            if command[-2:-1] == [">"]:
                output = self.cwd / command[-1]
                command = command[:-2]
            program, *args = command
            if program == "cd":
                self.cwd = self.cwd / args[0]
                continue
            if program == "read":  # read -rsp "Prompt: " NAME: the user types it.
                assert args[0] == "-rsp", line
                assert len(args) == 3, line
                self.variables[args[2]] = TYPED[args[2]]
                continue
            if program == "unset":  # Forgotten: no later line's process gets it.
                for name in args:
                    del self.variables[name]
                    self.exported.discard(name)
                continue
            if program == "export":
                assert all(name in self.variables for name in args), line
                self.exported.update(args)
                continue
            argv = {
                "domainkit": [sys.executable, "-m", "domainkit"],
                "python": [sys.executable],
                "pytest": [sys.executable, "-m", "pytest", "-q", *QUIET],
                "ruff": [sys.executable, "-m", "ruff"],
            }[program] + args
            done = subprocess.run(  # noqa: S603 - this interpreter, the page's line.
                argv, cwd=self.cwd, env=env, capture_output=True, text=True, check=False
            )
            assert done.returncode == 0, f"{line}\n{done.stdout}{done.stderr}"
            if output is not None:
                output.write_text(done.stdout, encoding="utf-8")
            else:
                self.printed[line] = self.printed.get(line, "") + done.stdout

    def expand(self, word: str) -> str:
        """Replace ``$NAME`` in a word with what the shell holds for it."""
        return re.sub(r"\$([A-Z_]+)\b", lambda name: self.variables[name[1]], word)

    def requests(self, lines: list[str]) -> list[dict[str, Any]]:
        """Send the ``curl`` lines to the application, as a server would get them."""
        results = self.cwd.parent / "walkthrough-results.jsonl"
        given = {
            "lines": lines,
            "typed": TYPED,
            "variables": self.variables,
            "exported": sorted(self.exported),
        }
        done = subprocess.run(  # noqa: S603 - this interpreter and the driver.
            [sys.executable, str(DRIVER), str(results)],
            cwd=self.cwd,
            env=self.env,
            input=json.dumps(given),
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        *answered, state = [
            json.loads(result)
            for result in results.read_text(encoding="utf-8").splitlines()
        ]
        self.variables, self.exported = state["variables"], set(state["exported"])
        return answered

    def play(self, page: list[tuple[str, str]]) -> list[dict[str, Any]]:
        """Run a page's steps; return what each ``curl`` answered, and check it.

        A ``curl`` line with a trailing ``# NNN`` must be answered that
        status, and any other must succeed (2xx).
        """
        results: list[dict[str, Any]] = []
        serving: list[str] | None = None
        for where, step in [*page, ("$", "")]:
            if serving is not None and where == "$" and _SERVED.match(step):
                serving.append(step)
                continue
            if serving is not None:  # Ctrl-C: the server stops.
                answered = self.requests(serving)
                assert [result["line"] for result in answered] == serving
                results += answered
                serving = None
            if where != "$":
                walkthrough_edits.apply_edit(self.cwd / where, step)
            elif words(step) == ["python", "manage.py", "runserver"]:
                serving = []
            elif step:
                self.run(step)
        results = [result for result in results if result["status"] is not None]
        for result in results:
            expected = result["expected"]
            if expected is None:
                assert 200 <= result["status"] < 300, result
            else:
                assert result["status"] == expected, result
        return results


def without_environment(page: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The page's steps without its environment lines, which must be ours."""
    skipped = [
        step for where, step in page if where == "$" and step in ENVIRONMENT_LINES
    ]
    assert skipped == list(ENVIRONMENT_LINES)
    return [(where, step) for where, step in page if step not in ENVIRONMENT_LINES]


def test_the_quick_start_works_as_written(tmp_path: Path) -> None:
    """The README's commands, then its login and first request, as printed."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    lines = shell_lines(section(readme, "Quick start"))
    shell = Walk(tmp_path)

    login, listed = shell.play(without_environment([("$", line) for line in lines]))

    assert login["output"].startswith("dkt_")
    assert json.loads(listed["output"]) == {"items": [], "next_cursor": None}
    assert '`{"items":[],"next_cursor":null}`' in readme
    # startdomain prints the same four lines, for the user it cannot know.
    started = shell.printed[
        "python manage.py startdomain invoices --aggregate Invoice --minimal"
        "   # installs and mounts it"
    ]
    printed = [line.strip() for line in started.splitlines()[-4:]]
    printed = [line.replace("<username>", "admin") for line in printed]
    server = lines.index(
        "python manage.py runserver            # http://127.0.0.1:8000/docs"
    )
    assert printed == lines[server + 1 :]
    # The password is typed at a prompt: no line holds it, for shell history.
    assert not [line for line in lines if TYPED["PASSWORD"] in line]
    # Forgotten once the superuser exists: runserver does not inherit it.
    assert "PASSWORD" not in shell.env


@dataclass
class Tutorial:
    """The getting-started path, walked once: the shell, and each part's answers."""

    shell: Walk
    answers: dict[str, list[dict[str, Any]]]


@pytest.fixture(scope="module")
def tutorial(tmp_path_factory: pytest.TempPathFactory) -> Tutorial:
    """Walk every part in order, in one directory, as one reader would."""
    assert sorted(path.name for path in TUTORIAL.glob("*.md")) == list(PARTS)
    shell = Walk(tmp_path_factory.mktemp("tutorial"))
    answers: dict[str, list[dict[str, Any]]] = {}
    for part in PARTS:
        page = steps((TUTORIAL / part).read_text(encoding="utf-8"))
        if part == PARTS[0]:
            page = without_environment(page)
        answers[part] = shell.play(page)
    return Tutorial(shell, answers)


def body(answer: dict[str, Any]) -> Any:  # noqa: ANN401 - JSON.
    """The JSON a ``curl`` line printed, after the headers ``-i`` adds."""
    output: str = answer["output"]
    if output.startswith("HTTP/1.1 "):
        output = output.split("\n\n", 1)[1]
    return json.loads(output)


def test_part_1_serves_a_first_resource(tutorial: Tutorial) -> None:
    """Install to a created and listed account, behind a login."""
    anonymous, login, created, kept, blank = tutorial.answers[PARTS[0]]

    assert body(anonymous)["code"] == "not_authenticated"
    assert login["output"].startswith("dkt_")
    assert created["output"].startswith("HTTP/1.1 201 Created\n")
    assert "\nlocation: /api/v1/accounts/accounts/" in created["output"]
    assert body(created)["name"] == "Savings"
    assert kept["output"].strip() == body(created)["id"]
    assert body(blank)["code"] == "invalid_account_name"


def test_part_2_holds_the_rules_written_by_hand(tutorial: Tutorial) -> None:
    """A deposit adds to the balance; the aggregate and the API refuse the rest."""
    answers = tutorial.answers[PARTS[1]]
    deposited, negative, too_precise, closed, archived, late = answers

    assert body(deposited)["balance"] == "100.00"
    assert body(deposited)["id"] == tutorial.shell.variables["ID"]
    assert body(negative)["code"] == "invalid_amount"
    assert body(too_precise)["code"] == "validation_error"
    assert body(too_precise)["errors"][0]["loc"] == ["body", "amount"]
    assert closed["output"].strip() == tutorial.shell.variables["CLOSED"]
    assert body(archived)["archived"] is True
    assert body(late)["code"] == "account_is_archived"


def test_part_3_grants_a_clerk_only_what_they_need(tutorial: Tutorial) -> None:
    """403 without the permission, reads with it, and still no deposits."""
    answers = tutorial.answers[PARTS[2]]
    made, login, refused, granted, listed, deposit = answers

    assert made["status"] == 201
    assert login["output"].startswith("dkt_")
    assert body(refused)["code"] == "permission_denied"
    assert body(granted)["permissions"] == ["accounts.view_account"]
    assert sorted(item["name"] for item in body(listed)["items"]) == [
        "Old savings",
        "Savings",
    ]
    assert body(deposit)["code"] == "permission_denied"
    # Typed at prompts, and forgotten: no later process inherits a password.
    assert not {"PASSWORD", "CLERK_PASSWORD"} & set(tutorial.shell.variables)


def test_part_4_runs_a_durable_handler_in_the_worker(tutorial: Tutorial) -> None:
    """The deposit's reaction waits in the outbox until the worker runs it."""
    [deposited] = tutorial.answers[PARTS[3]]
    printed = tutorial.shell.printed

    assert body(deposited)["balance"] == "125.00"
    pending = printed["python manage.py messages --pending"].splitlines()
    assert [line.split()[2].rsplit(".", 1)[1] for line in pending] == [
        "log_account_created",
        "log_account_created",
        "log_deposit",
    ]
    ran = printed["python manage.py runworker --once"]
    account = tutorial.shell.variables["ID"]
    assert f"Deposited 25.00 into account {account}" in ran


def test_part_5_tests_the_rules_and_passes_the_production_checks(
    tutorial: Tutorial,
) -> None:
    """The project's tests pass with the new ones; production settings check out."""
    assert tutorial.answers[PARTS[4]] == []
    tests = tutorial.shell.cwd / "tests" / "domains" / "accounts"
    assert "def test_deposits_must_be_positive" in (tests / "test_model.py").read_text()
    printed = tutorial.shell.printed["pytest"]
    # Twice: after part 2, and with part 5's three tests (one of them twice).
    first, second = re.findall(r"(\d+) passed", printed)
    assert int(second) == int(first) + 4
    [check] = [
        output
        for line, output in tutorial.shell.printed.items()
        if line.startswith("BANK_API_SETTINGS_MODULE=config.settings.production ")
    ]
    assert "no issues and 2 warnings" in check
    for warned in ("deploy.W004", "deploy.W006"):
        assert f"Warning: {warned}: " in check
    assert "auth." not in check  # Guesses are counted in Redis (auth.E105).


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("curl --fail http://127.0.0.1:8000/health", "is not understood"),
        ("curl https://example.com/", "needs a URL under"),
        ("curl http://127.0.0.1:8000/a http://127.0.0.1:8000/b", "is not understood"),
        ("wget http://127.0.0.1:8000/health", "not a curl command"),
        ("curl http://127.0.0.1:8000/$NOPE", "$NOPE was never set"),
        ("X=$(curl -s http://127.0.0.1:8000/health | jq .)", "may be piped"),
        ("X=$(curl -s http://127.0.0.1:8000/health)", "one curl, piped"),
        ('read -rsp "Password: " NOPE && export NOPE', "nothing was typed"),
    ],
)
def test_the_driver_refuses_what_it_would_not_run_as_a_shell(
    line: str, message: str
) -> None:
    """A page that drifts from the understood shapes fails instead of passing."""
    from domainkit_tests import walkthrough_driver

    with pytest.raises(walkthrough_driver.Refused, match=re.escape(message)):
        walkthrough_driver.run_line(None, line, {})


def test_the_driver_forgets_an_unset_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``unset NAME`` drops it for the lines, and the processes, that follow."""
    from domainkit_tests import walkthrough_driver

    monkeypatch.setenv("PASSWORD", TYPED["PASSWORD"])
    variables = {"PASSWORD": TYPED["PASSWORD"], "TOKEN": "dkt_kept"}

    result = walkthrough_driver.run_line(None, "unset PASSWORD", variables)

    assert result == {
        "line": "unset PASSWORD",
        "status": None,
        "expected": None,
        "output": "",
    }
    assert variables == {"TOKEN": "dkt_kept"}
    assert "PASSWORD" not in os.environ
    with pytest.raises(walkthrough_driver.Refused, match=re.escape("$PASSWORD")):
        walkthrough_driver.run_line(
            None, "curl http://127.0.0.1:8000/$PASSWORD", variables
        )


ORIGINAL = '''"""A module a generator wrote."""

from decimal import Decimal


class Account:
    """An account."""

    name: str
    archived: bool = False

    def deposit(self, amount: Decimal) -> None:
        """Record it."""
        self.record(amount)


def helper() -> int:
    """One."""
    return 1
'''


def test_an_edit_goes_where_the_page_says(tmp_path: Path) -> None:
    """Imports join the imports; definitions replace or join their namesakes."""
    module = tmp_path / "model.py"
    module.write_text(ORIGINAL, encoding="utf-8")
    edit = '''from decimal import Decimal
from typing import Final


class Account:
    ...
    balance: Decimal = Decimal("0.00")

    def deposit(self, amount: Decimal) -> None:
        """Add it."""
        self.balance += amount

    @property
    def open(self) -> bool:
        """Not archived."""
        return not self.archived


def helper() -> int:
    """Two."""
    return 2


class Refusal(Exception):
    """New."""


LIMIT: Final = 5
'''

    walkthrough_edits.apply_edit(module, edit)

    assert (
        module.read_text(encoding="utf-8")
        == '''"""A module a generator wrote."""

from decimal import Decimal
from typing import Final


class Account:
    """An account."""

    name: str
    archived: bool = False
    balance: Decimal = Decimal("0.00")

    def deposit(self, amount: Decimal) -> None:
        """Add it."""
        self.balance += amount

    @property
    def open(self) -> bool:
        """Not archived."""
        return not self.archived


def helper() -> int:
    """Two."""
    return 2


class Refusal(Exception):
    """New."""


LIMIT: Final = 5
'''
    )


def test_an_edit_finds_the_file_a_command_named(tmp_path: Path) -> None:
    """``*`` stands for a migration's revision; a replaced method keeps its depth."""
    (tmp_path / "0002_3ffd_balance.py").write_text(ORIGINAL, encoding="utf-8")

    walkthrough_edits.apply_edit(
        tmp_path / "0002_*_balance.py",
        'def deposit(self, amount: Decimal) -> None:\n    """No."""\n',
    )

    text = (tmp_path / "0002_3ffd_balance.py").read_text(encoding="utf-8")
    assert (
        '    def deposit(self, amount: Decimal) -> None:\n        """No."""\n' in text
    )
    assert "self.record" not in text


@pytest.mark.parametrize(
    ("files", "target", "edit", "message"),
    [
        ([], "model.py", "x = 1\n", "does not exist"),
        ([], "0002_*_x.py", "x = 1\n", "0 files match"),
        (["0002_a_x.py", "0002_b_x.py"], "0002_*_x.py", "x = 1\n", "2 files match"),
        (["model.py"], "model.py", "class Missing:\n    ...\n    x: int\n", "Missing"),
        (["model.py"], "model.py", "class helper:\n    ...\n    x: int\n", "helper"),
    ],
)
def test_an_edit_that_cannot_be_placed_is_refused(
    tmp_path: Path, files: list[str], target: str, edit: str, message: str
) -> None:
    """No file, several, or no class to add to: the page is wrong, not the harness."""
    for name in files:
        (tmp_path / name).write_text(ORIGINAL, encoding="utf-8")

    with pytest.raises(walkthrough_edits.Refused, match=re.escape(message)):
        walkthrough_edits.apply_edit(tmp_path / target, edit)


def test_a_new_file_is_written_as_the_block_holds_it(tmp_path: Path) -> None:
    """`` (new file)`` after the name: created, with its directories; never twice."""
    target = tmp_path / "tests" / "test_new.py (new file)"

    walkthrough_edits.apply_edit(target, ORIGINAL)

    assert (tmp_path / "tests" / "test_new.py").read_text(encoding="utf-8") == (
        ORIGINAL
    )
    with pytest.raises(walkthrough_edits.Refused, match="exists already"):
        walkthrough_edits.apply_edit(target, "x = 1\n")


def test_an_edit_of_a_name_defined_twice_is_refused(tmp_path: Path) -> None:
    """Which ``deposit`` the page means is for it to say."""
    module = tmp_path / "model.py"
    module.write_text(
        ORIGINAL + "\n\nclass Other:\n    def deposit(self) -> None: ...\n"
    )

    with pytest.raises(walkthrough_edits.Refused, match="defined 2 times"):
        walkthrough_edits.apply_edit(module, "def deposit() -> None: ...\n")


def test_a_page_names_the_file_of_each_python_block() -> None:
    """A block without a file could go anywhere, so the page fails."""
    page = (
        '```bash\nls\n```\n\n```python title="a.py"\nx = 1\n```\n\n```text\nno\n```\n'
    )

    assert steps(page) == [("$", "ls"), ("a.py", "x = 1\n")]
    with pytest.raises(AssertionError, match="without a file"):
        steps("```python\nx = 1\n```\n")
