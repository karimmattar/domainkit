"""Each line a change adds under ``src/`` runs in a test (ADR 0191, A28-TEST-05).

CI's coverage job, and ``make cov``, hold the runs combined to 98% of the
package, and every line a pull request adds or changes to 100%, its
branches included: ``.github/scripts/changed_coverage.py`` reads the diff
and the combined report.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from domainkit_tests.test_ci import ROOT, git, jobs, load_script, make_target

SCRIPT = ROOT / ".github" / "scripts" / "changed_coverage.py"

BEFORE = '''"""Prices."""


def total(prices: list[int]) -> int:
    """Their sum."""
    return sum(prices)
'''
AFTER = '''"""Prices."""


def total(prices: list[int]) -> int:
    """Their sum."""
    return sum(prices)


def discounted(price: int, member: bool) -> int:
    """Ten off for members."""
    if member:
        return price - 10
    return price
'''


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The script, imported."""
    return load_script("changed_coverage", monkeypatch)


def report(files: dict[str, dict[str, list[Any]]]) -> dict[str, Any]:
    """A JSON report of coverage, holding ``files`` (by path)."""
    return {"files": files}


def test_only_the_lines_a_hunk_adds_count(script: ModuleType) -> None:
    """A changed line, added ones, and none for a hunk that only deletes."""
    diff = textwrap.dedent(
        """\
        diff --git a/src/pkg/a.py b/src/pkg/a.py
        --- a/src/pkg/a.py
        +++ b/src/pkg/a.py
        @@ -3 +3 @@ def f():
        -    return 1
        +    return 2
        @@ -10,0 +11,3 @@ def g():
        +    a = 1
        +    b = 2
        +    return a + b
        @@ -20,2 +23,0 @@ def h():
        -    gone = 1
        -    return gone
        diff --git a/src/pkg/b.py b/src/pkg/b.py
        --- a/src/pkg/b.py
        +++ b/src/pkg/b.py
        @@ -1,2 +0,0 @@
        -x = 1
        -y = 2
        """
    )

    assert script.added_lines(diff) == {"src/pkg/a.py": {3, 11, 12, 13}}


def test_a_name_is_read_in_a_files_header_only(script: ModuleType) -> None:
    """A hunk's line that reads ``+++ b/...`` names nothing; a quoted name is read."""
    diff = textwrap.dedent(
        """\
        diff --git a/src/pkg/a.py b/src/pkg/a.py
        --- a/src/pkg/a.py
        +++ b/src/pkg/a.py
        @@ -1,0 +2,2 @@
        +x = 1
        +++ b/src/pkg/elsewhere.py
        @@ -9,0 +12 @@
        +y = 2
        diff --git "a/src/pkg/caf\\303\\251.py" "b/src/pkg/caf\\303\\251.py"
        --- "a/src/pkg/caf\\303\\251.py"
        +++ "b/src/pkg/caf\\303\\251.py"
        @@ -1 +1 @@
        -z = 0
        +z = 1
        diff --git a/src/pkg/old.py b/src/pkg/new.py
        similarity index 100%
        rename from src/pkg/old.py
        rename to src/pkg/new.py
        """
    )

    assert script.added_lines(diff) == {
        "src/pkg/a.py": {2, 3, 12},
        "src/pkg/caf\u00e9.py": {1},
    }


@pytest.mark.parametrize(
    "diff",
    [
        "diff --git src/a.py src/a.py\n--- src/a.py\n+++ src/a.py\n@@ -1 +1 @@\n+x\n",
        "diff --git a/src/a.py b/src/a.py\n@@ -1 +1 @@\n+x\n",
        "\x1b[1mdiff --git a/src/a.py b/src/a.py\x1b[m\n\x1b[36m@@ -1 +1 @@\x1b[m\n",
    ],
    ids=["no-prefix", "a-hunk-before-its-name", "colours"],
)
def test_a_diff_it_cannot_read_ends_it_unpassed(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, diff: str
) -> None:
    """Read as nothing, these passed whatever ran: refused, and the script ends 2."""
    with pytest.raises(script.UnreadDiff):
        script.added_lines(diff)
    monkeypatch.setattr(
        script.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=diff),
    )
    assert script.main(["HEAD^1", "no-report.json"]) == 2


def test_a_line_that_never_ran_or_left_a_branch_untaken_is_listed(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside ``src/``, or excluded, nothing; a file of code the report lacks."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "empty.py").write_text('"""Nothing."""\n')
    (tmp_path / "src" / "unseen.py").write_text("x = 1\n")
    measured = report(
        {
            "src/a.py": {
                "missing_lines": [5, 9],
                "missing_branches": [[7, 9], [12, -1]],
            },
            "src/covered.py": {"missing_lines": [], "missing_branches": []},
        }
    )
    added = {
        "src/a.py": {5, 6, 7, 12, 20},
        "src/covered.py": {1, 2},
        "src/empty.py": {1},
        "src/unseen.py": {1},
        "src/notes.md": {1},
        "domainkit_tests/test_a.py": {1},
    }

    assert script.unrun(added, measured) == {
        "src/a.py": [5, 7, 12],
        "src/unseen.py": None,
    }
    assert script.spans([1, 3, 4, 5, 9, 10]) == "1, 3-5, 9-10"


def scratch_repository(path: Path) -> None:
    """A repository at ``path`` whose ``src/prices.py`` is ``BEFORE``, committed."""
    (path / "src").mkdir()
    (path / "src" / "prices.py").write_text(BEFORE)
    git(path, "init", "-q", "-b", "main")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "before")


def measured_run(path: Path, *calls: str) -> subprocess.CompletedProcess[str]:
    """Run ``calls`` of ``src/prices.py`` under coverage, then the script on it."""
    (path / "use.py").write_text(
        "import sys\nsys.path.insert(0, 'src')\nimport prices\n" + "\n".join(calls)
    )
    # Not measured by this run's coverage: the child measures its own.
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("COVERAGE_")
    }
    for command in (
        ["run", "--branch", "--source=src", "use.py"],
        ["json", "-q", "-o", "coverage.json"],
    ):
        subprocess.run(  # noqa: S603 - this interpreter, coverage, fixed arguments.
            [sys.executable, "-m", "coverage", *command],
            cwd=path,
            env=environment,
            check=True,
            capture_output=True,
        )
    return subprocess.run(  # noqa: S603 - this interpreter and the script.
        [sys.executable, str(SCRIPT), "main", "coverage.json"],
        cwd=path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {
            "diff.noprefix": "true",
            "diff.mnemonicPrefix": "true",
            "color.diff": "always",
            "diff.external": "true",
        },
    ],
    ids=["git-as-it-comes", "git-set-otherwise"],
)
def test_a_change_passes_once_its_lines_and_branches_have_run(
    tmp_path: Path, settings: dict[str, str]
) -> None:
    """Members only: the line for others is listed, and the ``if`` it branches from.

    Whatever the repository sets for diffs: without prefixes, in colour, or
    through another tool, git printed what read as no change, and passed.
    """
    scratch_repository(tmp_path)
    for name, value in settings.items():
        git(tmp_path, "config", name, value)
    (tmp_path / "src" / "prices.py").write_text(AFTER)

    half = measured_run(tmp_path, "prices.discounted(100, True)")
    whole = measured_run(
        tmp_path, "prices.discounted(100, True)", "prices.discounted(100, False)"
    )

    assert (half.returncode, half.stdout) == (1, "src/prices.py: lines 11, 13\n")
    assert "ADR 0191" in half.stderr
    assert whole.returncode == 0, whole.stdout
    assert whole.stdout == "Every line added under src/ ran, each branch taken.\n"


def test_ci_and_make_cov_hold_a_change_to_every_line_it_adds() -> None:
    """On pull requests, against the base branch; ``make cov`` against ``stg``."""
    coverage = jobs()["coverage"]
    [checkout] = [
        step
        for step in coverage["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    [gate] = [
        step
        for step in coverage["steps"]
        if "changed_coverage.py" in step.get("run", "")
    ]
    makefile = (ROOT / "Makefile").read_text()

    assert checkout["with"]["fetch-depth"] == 2  # The merge commit and its base.
    # Pull requests to stg: the release's, from stg to main, holds the
    # whole release in its diff.
    assert gate["if"] == (
        "github.event_name == 'pull_request' && github.base_ref == 'stg'"
    )
    assert "changed_coverage.py HEAD^1 coverage.json" in gate["run"]
    assert "coverage json -q -o coverage.json --fail-under=0" in gate["run"]
    assert any("--fail-under=98" in each.get("run", "") for each in coverage["steps"])
    recipe = make_target("cov")
    assert (
        recipe[-1]
        == "$(RUN) python .github/scripts/changed_coverage.py $(BASE) coverage.json"
    )
    assert "BASE ?= origin/stg" in makefile
