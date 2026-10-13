"""Format generated Python files with ruff, when it is installed."""

import subprocess
import sys
from collections.abc import Iterable
from importlib.util import find_spec
from pathlib import Path


def format_python(paths: Iterable[Path]) -> bool:
    """Sort imports and format ``paths``; return whether ruff ran.

    Generated code (new domains, migrations) depends on names chosen at
    generation time, so it is formatted afterwards with the project's own
    ruff settings. Nothing happens when ruff is not installed.
    """
    if find_spec("ruff") is None:
        return False
    targets = [str(path) for path in paths]
    ruff = [sys.executable, "-m", "ruff"]
    for arguments in (
        ["check", "--fix", "--select", "I", "--quiet", *targets],
        ["format", "--quiet", *targets],
    ):
        subprocess.run([*ruff, *arguments], check=False)  # noqa: S603 - fixed program, generated paths.
    return True
