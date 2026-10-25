"""The README's quick start and the tutorial work exactly as written.

Each test reads the page's shell blocks and runs them in order in a new
directory: every command up to ``runserver`` as a user would, in a
subprocess of this interpreter; then every ``curl`` line against the
project's application, served in process by ``walkthrough_driver.py`` at
the address ``runserver`` would listen on. Only the lines that set up the
environment (a virtual environment and ``pip install``) are left out, and
they must be the ones listed here, so a changed page fails until this test
knows what it says.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
DRIVER = Path(__file__).with_name("walkthrough_driver.py")
ENVIRONMENT_LINES = (
    "python3.13 -m venv .venv && . .venv/bin/activate",
    'pip install "domainkit[postgres,sqlite,testing]"',
)
"""Lines that build the environment this interpreter already is."""
QUIET = ("-p", "no:cacheprovider")
"""The generated project's tests leave no cache behind."""
_BLOCK = re.compile(r"^```bash\n(.*?)^```$", re.DOTALL | re.MULTILINE)


def shell_lines(markdown: str) -> list[str]:
    """The commands of every ``bash`` block, continuation lines joined."""
    lines: list[str] = []
    for block in _BLOCK.findall(markdown):
        joined = re.sub(r"\\\n\s*", "", block)
        lines += [line.strip() for line in joined.splitlines() if line.strip()]
    return lines


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


class Walk:
    """A shell's state: where it is, and what it exports."""

    def __init__(self, directory: Path) -> None:
        """Start in ``directory``, with this process's environment, cleaned."""
        self.cwd = directory
        self.printed: dict[str, str] = {}
        """What each line printed, by line."""
        self.env = {
            name: value
            for name, value in os.environ.items()
            # A user's shell sets none of these: the tests' settings, the
            # generated projects' own, or mutmut's.
            if not name.startswith(("DOMAINKIT_", "BILLING_API_", "BANK_API_"))
            and name != "MUTANT_UNDER_TEST"
        }

    def run(self, line: str) -> None:
        """Run one line, as a shell would, and require it to succeed."""
        commands: list[list[str]] = [[]]
        for word in words(line):
            if word == "&&":
                commands.append([])
            else:
                commands[-1].append(word)
        for command in commands:
            env = dict(self.env)
            while "=" in command[0]:
                name, _, value = command.pop(0).partition("=")
                env[name] = value
            program, *args = command
            if program == "cd":
                self.cwd = self.cwd / args[0]
                continue
            argv = {
                "domainkit": [sys.executable, "-m", "domainkit"],
                "python": [sys.executable],
                "pytest": [sys.executable, "-m", "pytest", "-q", *QUIET],
            }[program] + args
            done = subprocess.run(  # noqa: S603 - this interpreter, the page's line.
                argv, cwd=self.cwd, env=env, capture_output=True, text=True, check=False
            )
            assert done.returncode == 0, f"{line}\n{done.stdout}{done.stderr}"
            self.printed[line] = self.printed.get(line, "") + done.stdout

    def requests(self, lines: list[str]) -> list[dict[str, Any]]:
        """Send the ``curl`` lines to the application, as a server would get them."""
        results = self.cwd.parent / "walkthrough-results.jsonl"
        done = subprocess.run(  # noqa: S603 - this interpreter and the driver.
            [sys.executable, str(DRIVER), str(results)],
            cwd=self.cwd,
            env=self.env,
            input=json.dumps(lines),
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        return [
            json.loads(result)
            for result in results.read_text(encoding="utf-8").splitlines()
        ]


def walk(lines: list[str], directory: Path) -> tuple[Walk, list[dict[str, Any]]]:
    """Run a page's lines; return the shell, and what each ``curl`` answered."""
    skipped = [line for line in lines if line in ENVIRONMENT_LINES]
    assert skipped == list(ENVIRONMENT_LINES)
    lines = [line for line in lines if line not in ENVIRONMENT_LINES]
    server = [words(line) for line in lines].index(["python", "manage.py", "runserver"])
    shell = Walk(directory)
    for line in lines[:server]:
        shell.run(line)
    results = shell.requests(lines[server + 1 :])
    assert [result["line"] for result in results] == lines[server + 1 :]
    for result in results:
        expected = result["expected"]
        if expected is None:
            assert 200 <= result["status"] < 300, result
        else:
            assert result["status"] == expected, result
    return shell, results


def test_the_quick_start_works_as_written(tmp_path: Path) -> None:
    """The README's commands, then its login and first request, as printed."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    lines = shell_lines(section(readme, "Quick start"))

    shell, (login, listed) = walk(lines, tmp_path)

    assert login["output"].startswith("dkt_")
    assert json.loads(listed["output"]) == {"items": [], "next_cursor": None}
    assert '`{"items":[],"next_cursor":null}`' in readme
    # startdomain prints the same two lines, for the user it cannot know.
    started = shell.printed[
        "python manage.py startdomain invoices --aggregate Invoice"
        "   # installs and mounts it"
    ]
    printed = [line.strip() for line in started.splitlines()[-2:]]
    credentials = {"<username>": "admin", "<password>": "correct-horse-battery"}
    for placeholder, value in credentials.items():
        printed = [line.replace(placeholder, value) for line in printed]
    assert printed == [login["line"], listed["line"]]


def test_the_tutorial_works_as_written(tmp_path: Path) -> None:
    """Every command of docs/tutorial.md, the refusals it shows included."""
    tutorial = (ROOT / "docs" / "tutorial.md").read_text(encoding="utf-8")

    _, results = walk(shell_lines(tutorial), tmp_path)

    anonymous, login, created, kept, noted, archived, late, blank = results
    assert json.loads(anonymous["output"])["code"] == "not_authenticated"
    assert login["output"].startswith("dkt_")
    assert created["output"].startswith("HTTP/1.1 201 Created\n")
    assert "\nlocation: /api/v1/accounts/accounts/" in created["output"]
    account = json.loads(created["output"].split("\n\n", 1)[1])
    assert kept["output"].strip() == account["id"]
    assert account["name"] == "Savings"
    assert json.loads(noted["output"])["notes"][0]["text"] == "Opened at the branch"
    assert json.loads(archived["output"])["archived"] is True
    assert json.loads(late["output"])["code"] == "account_is_archived"
    assert json.loads(blank["output"])["code"] == "invalid_account_name"


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("curl --fail http://127.0.0.1:8000/health", "is not understood"),
        ("curl https://example.com/", "needs a URL under"),
        ("curl http://127.0.0.1:8000/a http://127.0.0.1:8000/b", "is not understood"),
        ("wget http://127.0.0.1:8000/health", "not a curl command"),
        ("curl http://127.0.0.1:8000/$NOPE", "$NOPE was never set"),
        ("X=$(curl -s http://127.0.0.1:8000/health | jq .)", "may follow curl"),
    ],
)
def test_the_driver_refuses_what_it_would_not_run_as_a_shell(
    line: str, message: str
) -> None:
    """A page that drifts from the understood shapes fails instead of passing."""
    from domainkit_tests import walkthrough_driver

    with pytest.raises(walkthrough_driver.Refused, match=re.escape(message)):
        walkthrough_driver.run_line(None, line, {})
