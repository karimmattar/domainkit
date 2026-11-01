"""Whether main's CI run passed the checks a release needs, for release.yml.

A release reuses the CI run of its commit's push to ``main``. The run's
own conclusion counts every job, the advisory ones too: ``stress (3.14t)``
runs on that push and crashes now and then inside SQLAlchemy's compiled
extensions on 3.14t, which the ruleset leaves unrequired for that reason
(INFRA-14). So a release judges the run by the checks the main branch
ruleset requires (``.github/rulesets/main.json``, at the tagged commit),
and by ``attest``, which runs on pushes only and signs what is published.

Each of those must be in the run and have succeeded, except a check
another workflow reports (``codeql``, from codeql.yml, which the pull
request had to pass, and whose push runs a later push may cancel), and a
check a push never runs: ``changes`` and ``docs`` (pull requests only)
must have been skipped by their conditions. A required job skipped otherwise means a job
it needs failed, and that one is required too; failing it here as well is
the safe side. The run must have completed.

Reads ``gh run view <id> --json status,jobs`` from standard input and the
ruleset named, prints one line per check that did not pass, and exits 1
if any did not.

Usage: ``gh run view "$id" --json status,jobs | python3
.github/scripts/required_jobs.py .github/rulesets/main.json``.
"""

import json
import sys
from pathlib import Path
from typing import Any

SIGNING = ("attest",)
"""Jobs a release needs beyond the ruleset's: they run on pushes only."""
PULL_REQUEST_ONLY = frozenset({"changes", "docs"})
"""Required checks whose condition skips them on a push to main."""
OTHER_WORKFLOWS = frozenset({"codeql"})
"""Required checks of workflows other than ci.yml: not in its run."""


def required(ruleset: dict[str, Any]) -> list[str]:
    """The checks the ruleset requires, then the signing jobs."""
    contexts = [
        check["context"]
        for rule in ruleset["rules"]
        if rule["type"] == "required_status_checks"
        for check in rule["parameters"]["required_status_checks"]
    ]
    return [
        *(context for context in contexts if context not in OTHER_WORKFLOWS),
        *SIGNING,
    ]


def failures(run: dict[str, Any], checks: list[str]) -> list[str]:
    """Why each of ``checks`` did not pass in ``run``; empty if all did."""
    if run["status"] != "completed":
        return [f"the run is {run['status']}, not completed"]
    concluded = {job["name"]: job["conclusion"] for job in run["jobs"]}
    found: list[str] = []
    for check in checks:
        expected = "skipped" if check in PULL_REQUEST_ONLY else "success"
        conclusion = concluded.get(check)
        if conclusion is None:
            found.append(f"{check}: not in the run")
        elif conclusion != expected:
            found.append(f"{check}: {conclusion}, not {expected}")
    return found


def main(argv: list[str]) -> int:
    """Print what did not pass; exit 1 if anything did not."""
    ruleset = json.loads(Path(argv[0]).read_text())
    run = json.loads(sys.stdin.read())
    found = failures(run, required(ruleset))
    for line in found:
        print(line)  # noqa: T201 - the release's log.
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
