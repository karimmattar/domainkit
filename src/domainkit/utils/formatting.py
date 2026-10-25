"""Format generated Python files with ruff, when it is installed."""

import logging
import subprocess
import sys
from collections.abc import Iterable
from importlib.util import find_spec
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

UNFORMATTED: Final = (
    "ruff is not installed, so the generated code is not formatted: install "
    "the project's dev dependencies, or run 'ruff check --fix . && ruff format .'."
)


def format_python(paths: Iterable[Path]) -> bool:
    """Sort imports and format ``paths``; return whether ruff ran.

    Generated code (new domains, migrations) depends on names chosen at
    generation time, so it is formatted afterwards with the project's own
    ruff settings. Without ruff, nothing is formatted and a warning says
    how to do it. Without paths, nothing happens: ruff given none would
    format the whole working directory.
    """
    targets = [str(path) for path in paths]
    if not targets:
        return False
    if find_spec("ruff") is None:
        logger.warning(UNFORMATTED)
        return False
    ruff = [sys.executable, "-m", "ruff"]
    # No cache: it would land in a project that was just generated.
    for arguments in (
        ["check", "--fix", "--select", "I", "--quiet", "--no-cache", *targets],
        ["format", "--quiet", "--no-cache", *targets],
    ):
        subprocess.run([*ruff, *arguments], check=False)  # noqa: S603 - fixed program, generated paths.
    return True
