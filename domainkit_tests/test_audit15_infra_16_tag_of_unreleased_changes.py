"""INFRA-16: the release's version check passes a tag on unreleased changes.

``release.yml``'s tag job compares the tag with ``pyproject.toml``'s
version and nothing else. ``main`` has said ``0.13.0`` since the 0.13.0
release commit (2fac98d), through the 0.14 and 0.15 milestones, which were
never tagged, so ``v0.13.0`` pushed on any of four commits, ``d126394``
among them, passes it, and PyPI would get the 0.15 code as 0.13.0, whose
changelog section describes something else, with 0.14's and 0.15's
entries still under ``[Unreleased]``. ``docs/versioning.md`` step 1 moves
those entries under the version; nothing checks that it was done.

Here the tag job's own steps run on this commit's ``pyproject.toml``
and a changelog laid out as this commit's is, with ``poetry`` and
``pipx`` stood in for: the tag must be refused while ``[Unreleased]`` holds
entries, and pass once the changelog's newest section is the version's.
"""

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

from domainkit_tests.test_ci import ROOT, git, jobs

POETRY = """#!{python}
import sys, tomllib
assert sys.argv[1:] == ["version", "--short"], sys.argv
print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])
"""


def tag_job(repository: Path, tag: str, tmp_path: Path) -> int:
    """Run the tag job's steps in ``repository`` for ``tag``; its exit status."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "poetry").write_text(POETRY.format(python=sys.executable))
    (bin_dir / "pipx").write_text("#!/bin/sh\nexit 0\n")
    for shim in bin_dir.iterdir():
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    for step in jobs("release.yml")["tag"]["steps"]:
        if "run" not in step:
            continue
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "POETRY_VERSION": "2.2.1",
        }
        for name, value in step.get("env", {}).items():
            env[name] = str(value).replace("${{ github.ref_name }}", tag)
        done = subprocess.run(  # noqa: S603 - bash, the workflow's own step.
            ["bash", "-e", "-c", step["run"]],  # noqa: S607 - bash from PATH.
            cwd=repository,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode:
            return done.returncode
    return 0


def repository(path: Path, notes: str) -> Path:
    """A repository holding this commit's pyproject.toml and ``notes``, on main."""
    path.mkdir()
    (path / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text())
    (path / "CHANGELOG.md").write_text(notes)
    git(path, "init", "-q", "-b", "main")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "release")
    git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return path


def changelog(unreleased: str, version: str) -> str:
    """A changelog as this commit's is laid out: Unreleased, then ``version``."""
    return (
        "# Changelog\n\n## [Unreleased]\n\n"
        f"{unreleased}"
        f"## [{version}] - 2026-10-29\n\n### Removed\n\n- 0.12's deprecations.\n"
    )


def test_a_tag_on_unreleased_changes_is_refused(tmp_path: Path) -> None:
    """The version's section must be the newest, and Unreleased empty."""
    found = re.search(
        r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M
    )
    assert found is not None
    version = found.group(1)
    tag = f"v{version}"
    # Released properly: the entries moved under the version.
    released = changelog("", version)
    assert tag_job(repository(tmp_path / "released", released), tag, tmp_path) == 0
    # This commit's state: 0.14's and 0.15's entries are still unreleased.
    unreleased = changelog("### Added\n\n- domainkit.money (FIN-01).\n\n", version)
    assert tag_job(repository(tmp_path / "main", unreleased), tag, tmp_path) != 0
