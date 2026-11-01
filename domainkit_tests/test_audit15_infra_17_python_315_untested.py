"""INFRA-17: ``requires-python`` admits Python 3.15, which CI never runs.

Python 3.15.0 was released on 2026-10-09 (3.15.2 is out).
``requires-python = ">=3.13,<4"`` lets ``pip install domainkit`` onto it,
and ``docs/versioning.md`` says each release supports the versions
``requires-python`` admits, but the test matrix is 3.13 and 3.14 (and
3.14t), the classifiers name those two, and the newest and oldest jobs run
3.13 alone. A 1.0 should either test each released minor it admits, or
admit only those it tests.
"""

import tomllib

from packaging.specifiers import SpecifierSet

from domainkit_tests.test_ci import ROOT, jobs

# Each CPython minor released by now (3.15.0 on 2026-10-09).
RELEASED = ("3.13", "3.14", "3.15")


def test_every_released_python_it_admits_is_tested_and_classified() -> None:
    """The matrix and the classifiers name each minor ``requires-python`` admits."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    admitted = {
        minor
        for minor in RELEASED
        if SpecifierSet(project["requires-python"]).contains(f"{minor}.0")
    }
    tested = set(map(str, jobs()["test"]["strategy"]["matrix"]["python"]))
    classified = {
        each.removeprefix("Programming Language :: Python :: ")
        for each in project["classifiers"]
        if each.startswith("Programming Language :: Python :: 3.")
    }
    assert admitted <= tested
    assert admitted <= classified
