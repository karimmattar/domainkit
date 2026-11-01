"""INFRA-15: the immudb extra's protobuf floor has two advisories, unaudited.

Every floor in ``pyproject.toml`` is a version a project may install
(ADR 0075), and the comments there move a floor past each advisory
(anyio, pyjwt, ecdsa). ``protobuf (>=5.27.0,<8.0.0)`` was left behind:
pip-audit finds PYSEC-2026-1806 (CVE-2025-4565, recursion in the pure-Python
parser; fixed in 5.29.5, 6.31.1) and PYSEC-2026-1805 (CVE-2026-0994,
``json_format.ParseDict`` recursion through ``Any``; fixed in 5.29.6,
6.33.5) in 5.27.0, the version CI's oldest job installs. That job never
runs pip-audit (only newest does), so nothing notices a floor that
an advisory overtakes.
"""

import re
import tomllib

from domainkit_tests.test_ci import ROOT, jobs

# The first protobuf of the 5.x line past both advisories.
PROTOBUF_PAST_ADVISORIES = (5, 29, 6)


def floor(name: str) -> tuple[int, ...]:
    """The floor of ``name``'s range in the extras, as numbers."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    for extra in project["optional-dependencies"].values():
        for requirement in extra:
            match = re.match(rf"{name}\s*\(?\s*>=\s*([0-9.]+)", requirement)
            if match:
                return tuple(int(part) for part in match.group(1).split("."))
    raise AssertionError(name)


def test_the_protobuf_floor_is_past_its_advisories() -> None:
    """``pip install domainkit[immudb]`` may not keep a vulnerable protobuf."""
    assert floor("protobuf") >= PROTOBUF_PAST_ADVISORIES


def test_the_oldest_job_audits_the_floors_it_installs() -> None:
    """As newest does: a floor an advisory overtakes is found the week it is."""
    oldest = [step.get("run", "") for step in jobs()["oldest"]["steps"]]
    assert any(".oldest/bin/python -m pip_audit" in each for each in oldest)
