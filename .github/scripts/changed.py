"""Whether a pull request changes anything but the docs, for CI's changes job.

Reads the changed paths, one per line, from standard input (``git diff
--name-only``) and prints ``code=true`` or ``code=false``, for
``$GITHUB_OUTPUT``. Docs are ``docs/**`` and the Markdown files at the top
of the repository (README.md, CHANGELOG.md, SECURITY.md). Anything else is
code, Markdown elsewhere included: the project templates' files ship in the
wheel. No paths at all is docs only: nothing for the suite to test.

Usage: ``git diff --name-only HEAD^1 HEAD | python3 .github/scripts/changed.py``.
"""

import sys


def is_docs(path: str) -> bool:
    """Whether ``path`` (relative to the repository) is documentation only."""
    return path.startswith("docs/") or ("/" not in path and path.endswith(".md"))


def main() -> int:
    """Print ``code=true`` if any path read is not documentation."""
    paths = [line for line in sys.stdin.read().splitlines() if line]
    code = any(not is_docs(path) for path in paths)
    print(f"code={str(code).lower()}")  # noqa: T201 - for $GITHUB_OUTPUT.
    return 0


if __name__ == "__main__":
    sys.exit(main())
