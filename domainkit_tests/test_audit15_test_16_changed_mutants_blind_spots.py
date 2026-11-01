"""TEST-16: ``mutation (changed)`` misses changes it says it mutates.

ADR 0106: the job "mutates the functions and methods the change touches"
(``.github/scripts/changed_mutants.py``). Three kinds of change get no
mutant, and the job reports "none changed" or fails for the wrong reason:

- a function with a decorator other than ``@staticmethod`` or
  ``@classmethod`` (a route, ``@property``, ``@contextmanager``,
  ``@model_validator``, ``@cache``, a pytest fixture): mutmut 3 never
  mutates one (``file_mutation.py``, "ignore decorated functions"), yet the
  script named it (it now lists it apart: mutmut cannot mutate it); a
  pull request that changes only such functions made ``mutmut run``
  stop with "Filtered for specific mutants, but nothing matches", and in
  a larger one they were dropped without a word. 363 of
  ``src/domainkit``'s 4,466 functions (3,494 lines) are decorated so;
- lines deleted inside a function that stays (a guard removed): a hunk
  ``+N,0`` adds no line, so the function is not named;
- a module moved and edited: ``--diff-filter=AM`` leaves out ``R``, which
  ``git diff`` reports with rename detection on (its default).

Fails on ``d126394``.
"""

import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGED_MUTANTS = ROOT / ".github" / "scripts" / "changed_mutants.py"

BEFORE = '''"""Payments."""

import contextlib


def guard(user):
    if not user.allowed:
        raise PermissionError(user)
    return user


@contextlib.contextmanager
def opened(limit):
    yield limit + 1
'''


def git(cwd: Path, *args: str) -> None:
    """Run git in ``cwd``."""
    subprocess.run(  # noqa: S603 - our own arguments.
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def changed(tmp_path: Path, edit: str) -> list[str]:
    """The patterns ``changed_mutants.py`` prints after ``edit`` of the module."""
    package = tmp_path / "src" / "domainkit"
    package.mkdir(parents=True)
    (package / "pay.py").write_text(BEFORE, encoding="utf-8")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    if edit == "move":
        git(tmp_path, "mv", "src/domainkit/pay.py", "src/domainkit/payments.py")
        moved = package / "payments.py"
        moved.write_text(
            moved.read_text(encoding="utf-8").replace(
                "PermissionError(user)", "PermissionError(user.id)"
            ),
            encoding="utf-8",
        )
    else:
        module = package / "pay.py"
        old, new = edit.split("|")
        module.write_text(
            module.read_text(encoding="utf-8").replace(old, new), encoding="utf-8"
        )
    git(tmp_path, "add", ".")
    done = subprocess.run(  # noqa: S603 - this interpreter and the script.
        [sys.executable, str(CHANGED_MUTANTS), "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.split()


def mutmuts_names(source: str) -> list[str]:
    """The names mutmut gives the mutants of ``source``, as a module."""
    script = (
        "import sys\n"
        "from mutmut.mutation.file_mutation import mutate_file_contents\n"
        "print(' '.join(mutate_file_contents('pay.py', sys.stdin.read()).mutant_names))"
    )
    done = subprocess.run(  # noqa: S603 - this interpreter, our script.
        [sys.executable, "-c", script],
        cwd=ROOT,  # mutmut reads its settings from pyproject.toml.
        input=source,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.split()


def test_every_changed_function_named_has_mutants(tmp_path: Path) -> None:
    """A decorated function mutmut never mutates is listed apart, not named.

    The decision (0.15 fix wave): mutmut 3 cannot mutate it, so the script
    names only what mutmut mutates, and lists the rest in
    ``unmutated-functions.txt`` for the job summary; ADR 0106 says so.
    """
    patterns = changed(tmp_path, "limit + 1|limit + 2")
    names = [
        f"domainkit.pay.{name}"
        for name in mutmuts_names(BEFORE.replace("limit + 1", "limit + 2"))
    ]
    listed = (tmp_path / "unmutated-functions.txt").read_text(encoding="utf-8")

    assert patterns == []
    assert all(any(fnmatchcase(n, p) for n in names) for p in patterns), names
    assert listed == "domainkit.pay.x_opened\n"
    assert not any(name.startswith("domainkit.pay.x_opened") for name in names)


def test_a_deleted_guard_mutates_its_function(tmp_path: Path) -> None:
    """Removing a check from a function that stays changes that function."""
    removed = "    if not user.allowed:\n        raise PermissionError(user)\n"

    assert changed(tmp_path, f"{removed}|") == ["domainkit.pay.x_guard__mutmut_*"]


def test_a_moved_and_edited_module_is_mutated(tmp_path: Path) -> None:
    """A module moved in the same pull request still has its changes mutated."""
    assert changed(tmp_path, "move") == ["domainkit.payments.x_guard__mutmut_*"]
