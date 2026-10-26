"""CI's workflows, read as YAML: what runs when, and what the rulesets require.

The weekly mutation run covers every module, each in exactly one shard, in
a concurrency group no push shares, and reports a score even when it runs
out of time; a hung test ends the suite well within its job's time; the
main branch ruleset requires every job that checks a pull request; actions
are pinned to commits and images to digests, and Renovate sees the images.
"""

import ast
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RULESETS = ROOT / ".github" / "rulesets"
SOURCE = ROOT / "src"
SUMMARY = ROOT / ".github" / "scripts" / "mutation_summary.py"
FLOORS = ROOT / ".github" / "scripts" / "floors.py"
NOTES = ROOT / ".github" / "scripts" / "release_notes.py"
EVENTS = ("push", "pull_request", "schedule", "workflow_dispatch")
# GitHub Actions, the app whose checks the ruleset accepts.
ACTIONS_APP = 15368
# Jobs that run on pull requests but gate nothing: the newest and oldest
# releases the ranges allow are not this change's doing.
ADVISORY = {"newest", "oldest"}
# GitHub stops a job after six hours.
JOB_LIMIT_MINUTES = 360


def workflow(name: str = "ci.yml") -> dict[str, Any]:
    """The workflow, as YAML reads it."""
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text())
    return loaded


def jobs(name: str = "ci.yml") -> dict[str, dict[str, Any]]:
    """The workflow's jobs, by id."""
    found: dict[str, dict[str, Any]] = workflow(name)["jobs"]
    return found


def evaluate(expression: str, github: dict[str, str]) -> bool | str:
    """The value of a GitHub expression of the kinds the workflows use.

    ``||`` of ``&&`` of comparisons (``==``, ``!=``) between ``github.*``
    values and quoted strings, inside ``${{ }}`` or bare, as ``if:`` allows;
    other text is kept, so ``ci-${{ github.ref }}`` reads as a string.
    """
    whole = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", expression, re.DOTALL)
    if whole is None and "${{" in expression:
        return re.sub(
            r"\$\{\{(.*?)\}\}",
            lambda match: str(evaluate(match.group(1), github)),
            expression,
        )
    text = whole.group(1) if whole else expression

    def term(atom: str) -> str:
        atom = atom.strip()
        if atom.startswith("'"):
            return atom.strip("'")
        return github[atom.removeprefix("github.")]

    def comparison(part: str) -> bool | str:
        if "!=" in part:
            left, right = part.split("!=")
            return term(left) != term(right)
        if "==" in part:
            left, right = part.split("==")
            return term(left) == term(right)
        return term(part)

    alternatives = [
        [comparison(part) for part in alternative.split("&&")]
        for alternative in text.split("||")
    ]
    if len(alternatives) == 1 and len(alternatives[0]) == 1:
        return alternatives[0][0]
    return any(all(parts) for parts in alternatives)


def context(event: str) -> dict[str, str]:
    """``github.*`` for a run of ``event`` on the main branch."""
    return {"event_name": event, "ref": "refs/heads/main"}


def runs(job: dict[str, Any], event: str) -> bool:
    """Whether ``job`` runs for ``event``, its ``needs`` having run."""
    condition = job.get("if")
    return condition is None or evaluate(condition, context(event)) is True


def needs(job: dict[str, Any]) -> set[str]:
    """The jobs ``job`` waits for: ``needs`` is one name or a list."""
    wanted = job.get("needs", [])
    return {wanted} if isinstance(wanted, str) else set(wanted)


def running(event: str) -> set[str]:
    """The jobs a run of ``event`` starts: those whose condition and needs hold."""
    found: set[str] = set()
    pending = jobs()
    while True:
        ready = {
            name
            for name, job in pending.items()
            if name not in found and runs(job, event) and needs(job) <= found
        }
        if not ready:
            return found
        found |= ready


def check_names(name: str, job: dict[str, Any]) -> Iterator[str]:
    """The checks a job reports: its name, once per matrix entry."""
    label = job.get("name", name)
    matrix = job.get("strategy", {}).get("matrix", {})
    match = re.search(r"\$\{\{ matrix\.(\w+) \}\}", label)
    if match is None:
        yield label
        return
    for value in matrix[match.group(1)]:
        yield label.replace(match.group(0), str(value))


def shards() -> dict[str, list[str]]:
    """Each shard of the mutation job, and the mutant names it runs."""
    matrix = jobs()["mutation"]["strategy"]["matrix"]["shard"]
    return {shard["name"]: shard["mutants"].split() for shard in matrix}


def mutated_modules() -> list[str]:
    """Every module with a function or class mutmut can mutate."""
    modules = []
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        if not any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            for node in tree.body
        ):
            continue
        parts = path.relative_to(SOURCE).with_suffix("").parts
        modules.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return modules


def test_every_mutated_module_is_in_exactly_one_shard() -> None:
    """No module is left out of the weekly run, or mutated twice."""
    patterns = [
        (shard, pattern) for shard, names in shards().items() for pattern in names
    ]
    assert len(shards()) == len(jobs()["mutation"]["strategy"]["matrix"]["shard"])
    for module in mutated_modules():
        mutant = f"{module}.x_f__mutmut_1"  # How mutmut names a mutant.
        matching = [
            shard for shard, pattern in patterns if fnmatchcase(mutant, pattern)
        ]
        assert len(matching) == 1, (module, matching)


def test_every_pattern_names_a_module() -> None:
    """A pattern that matches nothing is a typo, or a module that moved."""
    modules = mutated_modules()
    for shard, names in shards().items():
        assert names, shard
        for pattern in names:
            assert any(
                fnmatchcase(f"{module}.x_f__mutmut_1", pattern) for module in modules
            ), (shard, pattern)


def test_the_weekly_run_has_a_concurrency_group_no_push_shares() -> None:
    """A merge to main cancels the push before it, never the mutation run."""
    concurrency = workflow()["concurrency"]
    group = {event: evaluate(concurrency["group"], context(event)) for event in EVENTS}
    assert len(set(group.values())) == len(EVENTS), group
    cancels = {
        event: evaluate(concurrency["cancel-in-progress"], context(event))
        for event in EVENTS
    }
    assert cancels == {
        "push": True,
        "pull_request": True,
        "schedule": False,
        "workflow_dispatch": False,
    }


def test_each_event_starts_the_jobs_it_needs() -> None:
    """The weekly run mutates and audits; the rest check changes."""
    every = set(jobs())
    assert running("schedule") == {
        "audit",
        "semgrep",
        "newest",
        "oldest",
        "mutation",
    }
    assert running("pull_request") == every - {"mutation"}
    assert running("push") == every - {"mutation", "newest", "oldest"}
    assert running("workflow_dispatch") == every


def test_a_shard_reports_its_score_before_the_job_limit() -> None:
    """The run is interrupted in time to summarise and upload what it found."""
    job = jobs()["mutation"]
    assert job["timeout-minutes"] < JOB_LIMIT_MINUTES
    steps = {step.get("name", step.get("uses", "")): step for step in job["steps"]}
    run = steps["Run the shard's mutants"]["run"]
    match = re.search(r"timeout --signal=INT --kill-after=(\d+)m (\d+)m ", run)
    assert match, run
    kill_after, budget = int(match.group(1)), int(match.group(2))
    assert budget + kill_after + 15 <= job["timeout-minutes"]  # Setup and report.
    summary = steps["Summarise the shard"]
    assert summary["if"] == "${{ !cancelled() }}"
    assert ".github/scripts/mutation_summary.py" in summary["run"]
    assert "GITHUB_STEP_SUMMARY" in summary["run"]
    upload = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert upload["if"] == "${{ !cancelled() }}"
    assert set(upload["with"]["path"].split()) == {
        "mutmut-results.txt",
        "mutation-summary.json",
    }


def test_a_hung_test_ends_the_suite_well_within_its_jobs_time() -> None:
    """Two minutes a test, thirty a job; integration keeps its results on failure."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    options = config["tool"]["pytest"]["ini_options"]
    assert (options["timeout"], options["timeout_method"]) == (120, "thread")
    dev = config["dependency-groups"]["dev"]
    assert any(each.startswith("pytest-timeout ") for each in dev)
    for name in ("test", "integration", "newest", "oldest"):
        assert jobs()[name]["timeout-minutes"] == 30
    steps = jobs()["integration"]["steps"]
    assert any(
        "pytest --cov -rs --junitxml=junit.xml" in step.get("run", "") for step in steps
    )
    kept = [
        step
        for step in steps
        if step.get("if") == "always()"
        and step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert [step["with"]["name"] for step in kept] == ["test-results-integration"]
    assert "junit.xml" in kept[0]["with"]["path"].split()


def test_the_main_ruleset_requires_every_job_that_checks_a_pull_request() -> None:
    """Rename a job and the ruleset, and versioning.md, must follow."""
    ruleset = json.loads((RULESETS / "main.json").read_text())
    [rule] = [
        rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks"
    ]
    required = rule["parameters"]["required_status_checks"]
    assert {check["integration_id"] for check in required} == {ACTIONS_APP}
    contexts = [check["context"] for check in required]
    checked = {
        check
        for name in running("pull_request") - ADVISORY
        for check in check_names(name, jobs()[name])
    }
    assert sorted(contexts) == sorted(checked)
    versioning = (ROOT / "docs" / "versioning.md").read_text()
    for check in contexts:
        assert f"`{check}`" in versioning, check


def test_the_rulesets_hold_without_an_everyday_bypass() -> None:
    """Admins bypass main only in a pull request; nobody moves a release tag."""
    main = json.loads((RULESETS / "main.json").read_text())
    assert {actor["bypass_mode"] for actor in main["bypass_actors"]} == {"pull_request"}
    assert main["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    assert {"deletion", "non_fast_forward", "pull_request"} <= {
        rule["type"] for rule in main["rules"]
    }
    tags = json.loads((RULESETS / "release-tags.json").read_text())
    assert tags["bypass_actors"] == []
    assert tags["conditions"]["ref_name"]["include"] == ["refs/tags/v*"]
    assert {rule["type"] for rule in tags["rules"]} == {
        "update",
        "deletion",
        "non_fast_forward",
    }
    creation = json.loads((RULESETS / "release-tag-creation.json").read_text())
    assert creation["conditions"] == tags["conditions"]
    assert [rule["type"] for rule in creation["rules"]] == ["creation"]


@pytest.mark.parametrize("name", ["ci.yml", "release.yml", "codeql.yml"])
def test_actions_are_pinned_to_commits_and_tokens_only_read(name: str) -> None:
    """Every action by a full commit SHA; write access only where it is used."""
    assert workflow(name)["permissions"] == {"contents": "read"}
    for job_name, job in jobs(name).items():
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses is not None:
                # owner/repo, or an action in a folder of it, at a commit.
                assert re.fullmatch(r"[\w.-]+/[\w.-]+(/[\w.-]+)*@[0-9a-f]{40}", uses), (
                    uses
                )
        if name == "ci.yml":
            assert "permissions" not in job, job_name
    granted = {
        job_name: job["permissions"]
        for job_name, job in jobs(name).items()
        if "permissions" in job
    }
    if name == "release.yml":
        assert granted == {
            "attest": {
                "contents": "read",
                "id-token": "write",
                "attestations": "write",
            },
            "publish": {"id-token": "write"},
            "github-release": {"contents": "write"},
        }
    if name == "codeql.yml":
        assert granted == {"analyze": {"contents": "read", "security-events": "write"}}


def test_releases_publish_only_attested_files() -> None:
    """Provenance for the wheel and sdist, the bill of materials for the wheel."""
    release = jobs("release.yml")
    assert set(release["publish"]["needs"]) == {"ci", "attest"}
    attest = [
        step["with"]
        for step in release["attest"]["steps"]
        if step.get("uses", "").startswith("actions/attest@")
    ]
    assert attest == [
        {"subject-path": "dist/*"},
        {"subject-path": "dist/*.whl", "sbom-path": "sbom/sbom.cdx.json"},
    ]
    sbom = jobs()["sbom"]
    assert sbom["needs"] == "build"
    upload = sbom["steps"][-1]
    assert upload["with"]["name"] == "sbom"
    assert upload["with"]["path"] == "sbom.cdx.json"


def test_renovate_sees_every_image_ci_starts() -> None:
    """Each docker run image is pinned to a digest and matched by Renovate."""
    text = (WORKFLOWS / "ci.yml").read_text()
    services = {
        service["image"]
        for job in jobs().values()
        for service in job.get("services", {}).values()
    }
    pinned = re.findall(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", text)
    started = set(pinned) - services
    assert len(started) == 5, started
    config = json.loads((ROOT / "renovate.json").read_text())
    [manager] = [
        each
        for each in config["customManagers"]
        if each.get("depTypeTemplate") == "docker-run"
    ]
    [pattern] = manager["matchStrings"]
    found = {
        f"{m['depName']}:{m['currentValue']}@{m['currentDigest']}"
        for m in re.finditer(pattern.replace("(?<", "(?P<"), text)
    }
    assert found == started


def meta(directory: Path, exit_codes: dict[str, int | None]) -> None:
    """Write a mutmut meta file holding ``exit_codes``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "module.py.meta").write_text(
        json.dumps(
            {
                "exit_code_by_key": exit_codes,
                "durations_by_key": {},
                "estimated_durations_by_key": {},
            }
        )
    )


def summarise(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the mutation summary in ``cwd``."""
    return subprocess.run(  # noqa: S603 - the test interpreter and the repository's script.
        [sys.executable, str(SUMMARY), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_shard_summary_scores_only_its_own_mutants(tmp_path: Path) -> None:
    """Killed, timed out and type-caught count as caught; skipped are left out."""
    meta(
        tmp_path / "mutants" / "src" / "domainkit",
        {
            "domainkit.a.x_f__mutmut_1": 1,  # killed
            "domainkit.a.x_f__mutmut_2": 0,  # survived
            "domainkit.a.x_f__mutmut_3": 36,  # timeout
            "domainkit.a.x_f__mutmut_4": 37,  # caught by type check
            "domainkit.a.x_f__mutmut_5": 34,  # skipped
            "domainkit.a.x_f__mutmut_6": 33,  # no tests
            "domainkit.b.x_g__mutmut_1": None,  # another shard's, never run
        },
    )

    done = summarise(tmp_path, "a", "domainkit.a.*")

    assert done.returncode == 0, done.stderr
    result = json.loads((tmp_path / "mutation-summary.json").read_text())
    assert result == {
        "shard": "a",
        "mutants": 6,
        "counts": {
            "caught by type check": 1,
            "killed": 1,
            "no tests": 1,
            "skipped": 1,
            "survived": 1,
            "timeout": 1,
        },
        "score": 60.0,
        "complete": True,
    }
    assert (tmp_path / "mutmut-results.txt").read_text() == (
        "domainkit.a.x_f__mutmut_2: survived\ndomainkit.a.x_f__mutmut_6: no tests\n"
    )
    assert "### Mutation shard `a`" in done.stdout
    assert "6 mutants; score 60.0%" in done.stdout
    assert "| killed | 1 |" in done.stdout
    assert "2 mutants no test caught" in done.stdout
    assert "Incomplete" not in done.stdout


def test_a_shard_that_ran_out_of_time_fails_its_summary(tmp_path: Path) -> None:
    """Mutants never checked, or interrupted, mean no baseline: exit 1."""
    meta(
        tmp_path / "mutants",
        {
            "domainkit.a.x_f__mutmut_1": 1,
            "domainkit.a.x_f__mutmut_2": 2,  # interrupted
            "domainkit.a.x_f__mutmut_3": None,
        },
    )

    done = summarise(tmp_path, "a", "domainkit.a.*")

    assert done.returncode == 1
    result = json.loads((tmp_path / "mutation-summary.json").read_text())
    assert result["complete"] is False
    assert result["score"] == 100.0
    assert "**Incomplete:** 2 of 3 mutants were never checked" in done.stdout
    assert (tmp_path / "mutmut-results.txt").read_text() == ""


def test_a_shard_with_no_mutants_fails_its_summary(tmp_path: Path) -> None:
    """A pattern that matched nothing scores nothing, and says so."""
    (tmp_path / "mutants").mkdir()

    done = summarise(tmp_path, "a", "domainkit.a.*")

    assert done.returncode == 1
    assert "score n/a" in done.stdout
    assert summarise(tmp_path, "a").returncode == 2  # No pattern.


def floors(cwd: Path, pyproject: str) -> subprocess.CompletedProcess[str]:
    """Run the floors script on ``pyproject`` written in ``cwd``."""
    (cwd / "pyproject.toml").write_text(pyproject)
    return subprocess.run(  # noqa: S603 - the test interpreter and the repository's script.
        [sys.executable, str(FLOORS)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_range_has_a_floor_the_oldest_job_pins() -> None:
    """Each dependency and extra, once, at the version its range starts at."""
    done = subprocess.run(  # noqa: S603 - the test interpreter and the repository's script.
        [sys.executable, str(FLOORS), str(ROOT / "pyproject.toml")],
        capture_output=True,
        text=True,
        check=True,
    )
    pinned = dict(line.split("==") for line in done.stdout.splitlines())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = list(project["dependencies"])
    for extra in project["optional-dependencies"].values():
        declared += extra
    names = {re.split(r"[\s(\[;>=<]", each, maxsplit=1)[0].lower() for each in declared}
    assert set(pinned) == names
    assert pinned["fastapi"] == "0.143.1"
    assert pinned["grpcio"] == "1.66.2"  # The first with Python 3.13 wheels.
    assert "python .github/scripts/floors.py > floors.txt" in str(
        jobs()["oldest"]["steps"]
    )


def test_the_floors_script_reads_each_form_and_refuses_none(tmp_path: Path) -> None:
    """Poetry's parentheses, extras and markers; no floor, or two, is an error."""
    done = floors(
        tmp_path,
        "[project]\n"
        'dependencies = ["a[x] (>=1.2,<2)", "b>=0.3; python_version<\'4\'"]\n'
        '[project.optional-dependencies]\nc = ["c (>=5.0.0)", "b>=0.3"]\n',
    )
    assert (done.returncode, done.stdout) == (0, "a==1.2\nb==0.3\nc==5.0.0\n")
    done = floors(
        tmp_path,
        '[project]\ndependencies = ["a (<2)"]\n[project.optional-dependencies]\n',
    )
    assert done.returncode == 1
    assert "no >= floor in 'a (<2)'" in done.stderr
    done = floors(
        tmp_path,
        '[project]\ndependencies = ["a>=1"]\n'
        '[project.optional-dependencies]\nx = ["a>=2"]\n',
    )
    assert done.returncode == 1
    assert "a has two floors: 1 and 2" in done.stderr


def test_the_docs_site_builds_strictly_beside_the_tests() -> None:
    """Links and public names checked; the docs group only; nothing waits for it."""
    job = jobs()["docs-site"]
    runs = [step.get("run", "") for step in job["steps"]]
    assert "needs" not in job
    assert "poetry sync --only docs --no-root" in runs
    assert "poetry run mkdocs build --strict --site-dir site" in runs
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    docs = config["dependency-groups"]["docs"]
    assert sorted(re.split(r"[\s(]", each, maxsplit=1)[0] for each in docs) == [
        "mkdocs-material",
        "mkdocstrings-python",
    ]
    site = yaml.safe_load((ROOT / "mkdocs.yml").read_text())
    assert site["strict"] is True
    assert site["hooks"] == ["docs/hooks/site.py"]
    assert set(site["validation"].values()) == {"warn"}  # Failures, under --strict.


def test_the_workflows_are_audited_and_scanned() -> None:
    """Every workflow is audited by zizmor; CodeQL scans pull requests, weekly."""
    steps = [step.get("run", "") for step in jobs()["semgrep"]["steps"]]
    assert "zizmor --offline --format github .github/workflows" in steps
    # YAML 1.1 reads the key "on" as True.
    codeql: dict[object, Any] = yaml.safe_load((WORKFLOWS / "codeql.yml").read_text())
    assert set(codeql[True]) == {
        "push",
        "pull_request",
        "schedule",
        "workflow_dispatch",
    }
    uses = [step.get("uses", "") for step in codeql["jobs"]["analyze"]["steps"]]
    assert [each.split("@")[0] for each in uses] == [
        "actions/checkout",
        "github/codeql-action/init",
        "github/codeql-action/analyze",
    ]


def test_a_release_on_github_carries_notes_files_and_checksums() -> None:
    """After PyPI: the CHANGELOG's section, the files, the bill, their sums."""
    job = jobs("release.yml")["github-release"]
    run = "\n".join(step.get("run", "") for step in job["steps"])
    assert job["needs"] == "publish"
    assert '.github/scripts/release_notes.py "${TAG#v}" CHANGELOG.md' in run
    assert "sha256sum" in run
    assert "gh release create" in run
    assert "--verify-tag" in run
    newest = [step.get("run", "") for step in jobs()["newest"]["steps"]]
    assert any(".newest/bin/python -m pip_audit" in each for each in newest)


def test_the_release_notes_are_the_versions_section(tmp_path: Path) -> None:
    """Its entries without the heading; a version without entries is an error."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [1.2.0] - 2026-01-02\n\n"
        "### Added\n\n- A thing (X-1).\n\n## [1.1.0] - 2025-12-01\n\n- Old.\n"
        "\n## [1.0.0] - 2025-11-01\n\n",
        encoding="utf-8",
    )

    def notes(version: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - the test interpreter and the repository's script.
            [sys.executable, str(NOTES), version, str(changelog)],
            capture_output=True,
            text=True,
            check=False,
        )

    assert notes("1.2.0").stdout == "### Added\n\n- A thing (X-1).\n"
    assert notes("1.1.0").stdout == "- Old.\n"
    for version, problem in [("1.0.0", "is empty"), ("1.3.0", "no section")]:
        done = notes(version)
        assert done.returncode == 1
        assert problem in done.stderr
    done = notes("1.2")  # A prefix of 1.2.0 is not 1.2.0.
    assert done.returncode == 1


def ci_images() -> set[str]:
    """Every image CI starts: services, and ``docker run`` lines."""
    text = (WORKFLOWS / "ci.yml").read_text()
    return set(re.findall(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", text))


def make_target(name: str) -> list[str]:
    """The recipe lines of the Makefile's target ``name``, variables named."""
    makefile = (ROOT / "Makefile").read_text()
    match = re.search(rf"^{name}:[^\n]*\n((?:\t[^\n]*\n)*)", makefile, re.MULTILINE)
    assert match, name
    return [line.strip() for line in match[1].splitlines()]


def test_compose_runs_the_servers_ci_runs() -> None:
    """A laptop's servers are CI's, image for image, digest for digest."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    images = {service["image"] for service in compose["services"].values()}
    gitleaks = {image for image in ci_images() if "gitleaks" in image}

    assert images == ci_images() - gitleaks
    rabbitmq = compose["services"]["rabbitmq"]
    assert (
        "./.ci/rabbitmq/20-tls.conf:/etc/rabbitmq/conf.d/20-tls.conf:ro"
        in (rabbitmq["volumes"])
    )
    for service in ("immudb", "redis", "rabbitmq"):
        assert compose["services"][service]["depends_on"] == {
            "certs": {"condition": "service_completed_successfully"}
        }
    ports = [
        port for each in compose["services"].values() for port in each.get("ports", [])
    ]
    assert all(port.startswith("127.0.0.1:") for port in ports), ports


def test_the_contributor_commands_are_cis() -> None:
    """``make lint`` is the lint job; ``make integration`` sets its variables."""
    lint = [step["run"] for step in jobs()["lint"]["steps"] if "run" in step]
    commands = [
        line.replace("$(POETRY)", "poetry").replace("$(RUN)", "poetry run")
        for line in make_target("lint")
    ]
    assert commands == [
        each for each in lint if each.startswith("poetry ") and "sync" not in each
    ]
    makefile = (ROOT / "Makefile").read_text()
    block = makefile.split("INTEGRATION_ENV := \\\n", 1)[1].split("\n\n", 1)[0]
    variables = dict(
        line.strip().rstrip("\\").strip().split("=", 1) for line in block.splitlines()
    )
    assert variables == {
        name: str(value) for name, value in jobs()["integration"]["env"].items()
    }
    hooks = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())["repos"]
    local = [
        hook["entry"]
        for repo in hooks
        if repo["repo"] == "local"
        for hook in repo["hooks"]
    ]
    # Each hook runs a lint command, given the changed files instead of ".".
    assert [f"{entry} ." if f"{entry} ." in commands else entry for entry in local] == (
        commands
    )
    remote = [repo["rev"] for repo in hooks if repo["repo"] != "local"]
    assert all(re.fullmatch(r"[0-9a-f]{40}", rev) for rev in remote), remote
