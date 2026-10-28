"""CI's workflows, read as YAML: what runs when, and what the rulesets require.

The weekly mutation run covers every module, each in exactly one shard, in
a concurrency group no push shares, and reports a score even when it runs
out of time; a hung test ends the suite well within its job's time; the
main branch ruleset requires every job that checks a pull request; actions
are pinned to commits and images to digests, and Renovate sees the images.
"""

import ast
import json
import os
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
CHANGED = ROOT / ".github" / "scripts" / "changed.py"
CHANGED_MUTANTS = ROOT / ".github" / "scripts" / "changed_mutants.py"
EVENTS = ("push", "pull_request", "schedule", "workflow_dispatch")
# GitHub Actions, the app whose checks the ruleset accepts.
ACTIONS_APP = 15368
# Jobs that run on pull requests but gate nothing: the newest and oldest
# releases the ranges allow are not this change's doing.
ADVISORY = {"newest", "oldest", "free-threaded-stress", "mutation-changed"}
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


type Value = bool | str
_TOKEN = re.compile(
    r"\s*(?:(?P<string>'[^']*')|(?P<op>&&|\|\||==|!=|!|\(|\))"
    r"|(?P<name>[A-Za-z_][\w.-]*(?:\(\))?))"
)
STATUS = {"cancelled()": False, "failure()": False, "always()": True, "success()": True}
"""Status functions, as they read when every earlier job succeeded."""


def evaluate(expression: str, values: dict[str, str]) -> Value:
    """The value of a GitHub expression of the kinds the workflows use.

    ``||``, ``&&``, ``!``, parentheses, ``==`` and ``!=`` between context
    values (``github.ref``, ``needs.changes.outputs.code``) and quoted
    strings, and the status functions, inside ``${{ }}`` or bare, as
    ``if:`` allows. ``||`` and ``&&`` give an operand, as GitHub's do
    (``github.head_ref || github.sha``). Other text is kept, so
    ``ci-${{ github.ref }}`` reads as a string. A context value not given
    reads as GitHub's missing value, an empty string.
    """
    whole = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", expression, re.DOTALL)
    if whole is None and "${{" in expression:
        return re.sub(
            r"\$\{\{(.*?)\}\}",
            lambda match: str(evaluate(match.group(1), values)),
            expression,
        )
    tokens = _tokens(whole.group(1) if whole else expression)
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str:
        nonlocal position
        position += 1
        return tokens[position - 1]

    def either() -> Value:
        value = both()
        while peek() == "||":
            take()
            other = both()
            value = value or other
        return value

    def both() -> Value:
        value = compared()
        while peek() == "&&":
            take()
            other = compared()
            value = value and other
        return value

    def compared() -> Value:
        value = negated()
        if peek() in {"==", "!="}:
            equal = take() == "=="
            return (value == negated()) is equal
        return value

    def negated() -> Value:
        if peek() == "!":
            take()
            return not negated()
        if peek() == "(":
            take()
            value = either()
            assert take() == ")", expression
            return value
        atom = take()
        if atom.startswith("'"):
            return atom.strip("'")
        if atom in STATUS:
            return STATUS[atom]
        return values.get(atom.removeprefix("github."), "")

    value = either()
    assert position == len(tokens), expression
    return value


def _tokens(text: str) -> list[str]:
    """Split an expression into strings, operators, and names."""
    found: list[str] = []
    position = 0
    while text[position:].strip():
        match = _TOKEN.match(text, position)
        assert match, text[position:]
        found.append(match.group(match.lastgroup or ""))
        position = match.end()
    return found


def context(
    event: str, *, code: str = "true", sha: str = "abc", number: int = 1
) -> dict[str, str]:
    """``github.*`` for a run of ``event`` on the main branch.

    A pull request (``number``) comes from a branch (``head_ref``) and, by
    its changes job, changes ``code`` (``"true"``) or docs alone
    (``"false"``).
    """
    values = {"event_name": event, "ref": "refs/heads/main", "sha": sha}
    if event == "pull_request":
        values |= {
            "ref": f"refs/pull/{number}/merge",
            "head_ref": "feature",
            "needs.changes.outputs.code": code,
        }
    return values


def runs(job: dict[str, Any], values: dict[str, str], needed_ran: bool) -> bool:
    """Whether ``job`` runs: its condition, and its ``needs`` unless it says.

    A job is skipped when a job it needs, or one those need in turn, did
    not run, unless its condition calls a status function
    (``!cancelled()``), as GitHub decides: a condition without one is read
    as ``success() && ...``, and ``success()`` looks up the whole chain.
    """
    condition = job.get("if")
    if condition is None:
        return needed_ran
    if not any(function in condition for function in STATUS) and not needed_ran:
        return False
    return evaluate(str(condition), values) is True


def needs(job: dict[str, Any]) -> set[str]:
    """The jobs ``job`` waits for: ``needs`` is one name or a list."""
    wanted = job.get("needs", [])
    return {wanted} if isinstance(wanted, str) else set(wanted)


def running(event: str, *, code: str = "true") -> set[str]:
    """The jobs a run of ``event`` starts (for a pull request, by its changes)."""
    values = context(event, code=code)
    decided: dict[str, bool] = {}
    chain: dict[str, bool] = {}  # The job, and every job above it, ran.
    pending = jobs()
    while len(decided) < len(pending):
        for name, job in pending.items():
            if name in decided or not needs(job) <= set(decided):
                continue
            above = all(chain[need] for need in needs(job))
            decided[name] = runs(job, values, above)
            chain[name] = decided[name] and above
    return {name for name, ran in decided.items() if ran}


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
    """Only a pull request's new push cancels a run; main's are each kept.

    A release reuses the run of its commit's push to main, so none is ever
    cancelled, nor left pending behind another commit's.
    """
    concurrency = workflow()["concurrency"]
    group = {event: evaluate(concurrency["group"], context(event)) for event in EVENTS}
    assert len(set(group.values())) == len(EVENTS), group
    cancels = {
        event: evaluate(concurrency["cancel-in-progress"], context(event))
        for event in EVENTS
    }
    assert cancels == {
        "push": False,
        "pull_request": True,
        "schedule": False,
        "workflow_dispatch": False,
    }
    pushes = {evaluate(concurrency["group"], context("push", sha=sha)) for sha in "ab"}
    assert len(pushes) == 2
    reviews = {
        evaluate(concurrency["group"], context("pull_request", sha=sha)) for sha in "ab"
    }
    assert len(reviews) == 1
    # Two pull requests from branches of the same name (forks' "main").
    others = {
        evaluate(concurrency["group"], context("pull_request", number=number))
        for number in (1, 2)
    }
    assert len(others) == 2


def test_the_expression_reader_reads_what_github_would() -> None:
    """Operands, not booleans, from ``||`` and ``&&``; precedence; ``!``."""
    values = {"event_name": "push", "sha": "abc", "head_ref": ""}
    assert evaluate("github.head_ref || github.sha", values) == "abc"
    assert evaluate("${{ github.event_name == 'push' && 'yes' }}", values) == "yes"
    assert evaluate("!cancelled() && (github.sha == 'x' || 'y' != 'z')", values)
    assert evaluate("${{ !failure() && github.sha != 'abc' }}", values) is False
    assert evaluate("needs.changes.outputs.code != 'false'", values) is True
    assert evaluate("a-${{ github.sha }}-b", values) == "a-abc-b"


def test_each_event_starts_the_jobs_it_needs() -> None:
    """The weekly run mutates and audits; the rest check changes.

    A pull request that changes docs alone runs the cheap jobs, the docs
    job, and the test matrix, which skips its steps. Every push to main
    runs the whole suite: a release reuses that run.
    """
    every = set(jobs())
    assert running("schedule") == {
        "audit",
        "semgrep",
        "newest",
        "oldest",
        "mutation",
    }
    assert running("pull_request") == every - {"mutation", "docs", "attest"}
    assert "mutation-changed" in running("pull_request")
    assert running("pull_request", code="false") == {
        "changes",
        "docs",
        "docs-site",
        "lint",
        "test",
        "audit",
        "semgrep",
    }
    assert running("push") == every - {
        "mutation",
        "mutation-changed",
        "newest",
        "oldest",
        "changes",
        "docs",
    }
    assert running("workflow_dispatch") == every - {
        "changes",
        "docs",
        "attest",
        "mutation-changed",
    }


def test_a_docs_change_skips_the_test_matrix_steps_not_the_matrix() -> None:
    """Each check the ruleset names reports, so none waits forever.

    A job skipped by its condition reports success, but a matrix skipped
    whole reports one check under its unexpanded name; so the matrix runs,
    and every step of it is skipped for a docs-only change.
    """
    for name in running("pull_request", code="false"):
        job = jobs()[name]
        if "matrix" in job.get("strategy", {}):
            for step in job["steps"]:
                condition = step["if"]
                for code in ("true", "false"):
                    values = context("pull_request", code=code)
                    assert evaluate(condition, values) is (code == "true"), step
    for name in running("pull_request") - running("pull_request", code="false"):
        job = jobs()[name]
        assert "matrix" not in job.get("strategy", {}), name
        assert "changes" in needs(job), name


@pytest.mark.parametrize(
    ("paths", "code"),
    [
        ("", "false"),
        ("docs/tutorial.md\nREADME.md\nCHANGELOG.md\n", "false"),
        ("docs/adr/0081-x.md\ndocs/img/a.svg\n", "false"),
        ("docs/tutorial.md\nsrc/domainkit/clock.py\n", "true"),
        ("src/domainkit/management/templates/project/README.md\n", "true"),
        (".github/workflows/ci.yml\n", "true"),
        ("pyproject.toml\n", "true"),
        ("domainkit_tests/notes.md\n", "true"),
        ("M\tdocs/tutorial.md\nA\tREADME.md\n", "false"),
        ("D\tsrc/domainkit/http/guard.py\nA\tdocs/guard.py\n", "true"),
        ("R100\tsrc/domainkit/http/guard.py\tdocs/guard.py\n", "true"),
        ("R087\tdomainkit_tests/test_http.py\tdocs/test_http.py\n", "true"),
        ("R100\tdocs/guide.md\tsrc/domainkit/guide.md\n", "true"),
        ("R100\tdocs/old.md\tdocs/new.md\nC090\tREADME.md\tdocs/x.md\n", "false"),
        ('M\t"docs/\\303\\274.md"\n', "true"),
    ],
)
def test_only_docs_and_top_level_markdown_count_as_docs(paths: str, code: str) -> None:
    """Markdown in a package or the tests is code: templates ship in the wheel.

    Both sides of a rename or copy count: code moved into ``docs/`` left
    the package (SEC-10c-3). A path git quoted counts as code.
    """
    done = subprocess.run(  # noqa: S603 - the test interpreter and the repository's script.
        [sys.executable, str(CHANGED)],
        input=paths,
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == f"code={code}\n"
    [step] = [
        each
        for each in jobs()["changes"]["steps"]
        if "changed.py" in each.get("run", "")
    ]
    assert "git diff --no-renames --name-status HEAD^1 HEAD" in step["run"]


def git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd``, as a throwaway committer; return what it prints."""
    done = subprocess.run(  # noqa: S603 - git, fixed arguments.
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],  # noqa: S607 - git from PATH.
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


@pytest.mark.parametrize(
    ("moved", "edit"),
    [
        ("src/domainkit/http/guard.py", False),
        ("src/domainkit/http/guard.py", True),
        ("domainkit_tests/test_http.py", False),
    ],
)
def test_moving_code_into_the_docs_is_not_a_docs_change(
    tmp_path: Path, moved: str, edit: bool
) -> None:
    """The changes job's own command, on a pull request's merge commit.

    ``git diff --name-only`` reported a moved file under its new path
    alone, ``docs/...``: a pull request that moved a module (edited or
    not) or a test file into ``docs/`` skipped every test job
    (SEC-10c-3). Each such move must read as code.
    """
    [step] = [
        each
        for each in jobs()["changes"]["steps"]
        if "changed.py" in each.get("run", "")
    ]
    command = (
        step["run"]
        .replace(
            "python3 .github/scripts/changed.py", f'"{sys.executable}" "{CHANGED}"'
        )
        .replace('"$GITHUB_OUTPUT"', '"$OUTPUT"')
    )
    (tmp_path / Path(moved).parent).mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / moved).write_text((ROOT / moved).read_text(encoding="utf-8"))
    (tmp_path / "docs" / "index.md").write_text("# Docs\n")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "checkout", "-qb", "change")
    target = f"docs/{Path(moved).name}"
    git(tmp_path, "mv", moved, target)
    if edit:
        with (tmp_path / target).open("a") as file:
            file.write("# edited\n")
    git(tmp_path, "commit", "-qam", "move")
    git(tmp_path, "checkout", "-q", "main")
    git(tmp_path, "merge", "-q", "--no-ff", "-m", "merge", "change")
    assert "R" in git(tmp_path, "diff", "--name-status", "-M", "HEAD^1", "HEAD")

    output = tmp_path / "output"
    subprocess.run(  # noqa: S603 - bash, and the workflow's own command.
        ["bash", "-eo", "pipefail", "-c", command],  # noqa: S607 - bash from PATH.
        cwd=tmp_path,
        env={**os.environ, "OUTPUT": str(output)},
        check=True,
    )

    assert output.read_text() == "code=true\n"


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


def test_mutmut_copies_every_file_the_tests_read() -> None:
    """Every file a test reads, or a page links to, is in mutmut's copy.

    mutmut runs the suite in that copy, which holds ``source_paths``,
    ``pyproject.toml`` and ``also_copy``. A test reading anything else from
    the repository's root fails mutmut's first run of the suite, and with it
    every shard.
    """
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["mutmut"]
    copied = {"pyproject.toml"} | {
        path.rstrip("/") for path in [*config["source_paths"], *config["also_copy"]]
    }
    read = {
        match[1]
        for path in (ROOT / "domainkit_tests").rglob("*.py")
        for match in re.finditer(
            r'(?:\bROOT|parents\[1\])\s*/\s*"([^"/]+)', path.read_text(encoding="utf-8")
        )
    }
    assert read, "the pattern no longer finds what the tests read"
    # test_docs.py follows the pages' links to the repository's files.
    pages = [*(ROOT / "docs").rglob("*.md"), *ROOT.glob("*.md")]
    linked = {
        match[1]
        for page in pages
        for match in re.finditer(
            r"karimmattar/domainkit/(?:blob|tree)/main/([^/#)\s]+)",
            page.read_text(encoding="utf-8"),
        )
    }
    assert "LICENSE" in linked, "the pattern no longer finds what the pages link to"
    read |= linked
    assert read <= copied, f"add to [tool.mutmut] also_copy: {sorted(read - copied)}"


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
        "--cov -rs --junitxml=junit.xml" in step.get("run", "") for step in steps
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
    reviewed = running("pull_request") | running("pull_request", code="false")
    checked = {
        check
        for name in reviewed - ADVISORY
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
        if name == "ci.yml" and job_name != "attest":
            assert "permissions" not in job, job_name
    granted = {
        job_name: job["permissions"]
        for job_name, job in jobs(name).items()
        if "permissions" in job
    }
    if name == "release.yml":
        assert granted == {
            "ci": {"contents": "read", "actions": "read"},
            "publish": {
                "actions": "read",
                "attestations": "read",
                "id-token": "write",
            },
        }
    if name == "codeql.yml":
        assert granted == {"analyze": {"contents": "read", "security-events": "write"}}


def test_releases_publish_only_attested_files() -> None:
    """Provenance for the wheel and sdist, the bill of materials for the wheel.

    The run that built them attests them, so the provenance names it
    (ci.yml, on a push to main), not the release that only downloads them.
    Only that job, which checks out nothing, may mint an OIDC token or write
    attestations, and never for a pull request. The release checks both
    attestations of every file it publishes, signed by ci.yml on main at
    the tagged commit, before publishing.
    """
    attest = jobs()["attest"]
    assert attest["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    assert set(attest["needs"]) == {"build", "sbom"}
    assert attest["if"] == (
        "${{ !cancelled() && !failure() && github.event_name == 'push' }}"
    )
    assert running("pull_request") & {"attest"} == set()
    assert not any("checkout" in step.get("uses", "") for step in attest["steps"])
    assert not any("run" in step for step in attest["steps"])
    attested = [
        step["with"]
        for step in attest["steps"]
        if step.get("uses", "").startswith("actions/attest@")
    ]
    assert attested == [
        {"subject-path": "dist/*"},
        {"subject-path": "dist/*.whl", "sbom-path": "sbom/sbom.cdx.json"},
    ]
    release = jobs("release.yml")
    assert "attest" not in release
    assert set(release["publish"]["needs"]) == {"ci", "audit"}
    [check] = [
        step
        for step in release["publish"]["steps"]
        if "gh attestation verify" in step.get("run", "")
    ]
    publishing = [step.get("uses", "") for step in release["publish"]["steps"]]
    assert release["publish"]["steps"].index(check) < next(
        index
        for index, uses in enumerate(publishing)
        if uses.startswith("pypa/gh-action-pypi-publish@")
    )
    script = check["run"]
    assert script.count("gh attestation verify") == 2
    for option in (
        '--signer-workflow "$REPO/.github/workflows/ci.yml"',
        "--source-ref refs/heads/main",
        '--source-digest "$SHA"',
        "--deny-self-hosted-runners",
        "--predicate-type https://cyclonedx.org/bom",
    ):
        assert option in script, option
    assert check["env"]["SHA"] == "${{ github.sha }}"
    sbom = jobs()["sbom"]
    assert "build" in needs(sbom)
    # actions/attest takes a CycloneDX document only with a bomFormat, a
    # specVersion and a serialNumber, which --output-reproducible leaves out:
    # v0.9.2's release failed there.
    written = next(
        step["run"] for step in sbom["steps"] if "cyclonedx-py" in step.get("run", "")
    )
    assert "--of JSON" in written
    assert "--output-reproducible" not in written
    upload = sbom["steps"][-1]
    assert upload["with"]["name"] == "sbom"
    assert upload["with"]["path"] == "sbom.cdx.json"


def test_releases_reuse_mains_green_run_of_the_tagged_commit() -> None:
    """Nothing runs twice: the tag's push-to-main run must have succeeded.

    Its dist and sbom artifacts, attested by that run, are what is
    published; only the audit runs again, as CI's audit job does, since an
    advisory can appear after main's run.
    """
    release = jobs("release.yml")
    assert "workflow_call" not in (WORKFLOWS / "ci.yml").read_text()
    assert all("uses" not in job for job in release.values())
    [find] = release["ci"]["steps"]
    script = find["run"]
    for option in (
        "--workflow ci.yml",
        "--branch main",
        "--event push",
        '--commit "$SHA"',
    ):
        assert option in script, option
    assert find["env"]["SHA"] == "${{ github.sha }}"
    assert '[ "$conclusion" != "success" ]' in script
    assert release["ci"]["outputs"] == {"run-id": "${{ steps.run.outputs.id }}"}
    downloads = [
        step["with"]
        for step in release["publish"]["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    ]
    assert [each["name"] for each in downloads] == ["dist"]
    for each in downloads:
        assert each["run-id"] == "${{ needs.ci.outputs.run-id }}"
        assert each["github-token"] == "${{ github.token }}"
    for name in ("dist", "sbom"):
        [upload] = [
            step["with"]
            for step in jobs()["build" if name == "dist" else "sbom"]["steps"]
            if step.get("uses", "").startswith("actions/upload-artifact@")
            and step["with"]["name"] == name
        ]
        assert upload["retention-days"] == 90
    assert release["audit"]["steps"] == jobs()["audit"]["steps"]


def test_the_docs_job_runs_every_test_that_reads_the_docs() -> None:
    """A docs-only change runs each test module that opens a docs file.

    Those are the modules that join the repository's root to ``docs`` or a
    top-level Markdown file; a run with an audit hook on ``open`` found the
    same four (October 2026).
    """
    reading = re.compile(r'(?:ROOT|parents\[1\])\s*/\s*"(?:docs|[A-Za-z]+\.md)"')
    readers = {
        f"domainkit_tests/{path.name}"
        for path in (ROOT / "domainkit_tests").glob("test_*.py")
        if reading.search(path.read_text(encoding="utf-8"))
    }
    [command] = [
        step["run"]
        for step in jobs()["docs"]["steps"]
        if "pytest" in step.get("run", "")
    ]
    assert set(re.findall(r"domainkit_tests/\S+\.py", command)) == readers
    assert len(readers) == 4


def test_the_suite_runs_on_every_core() -> None:
    """pytest-xdist in the dev group; ``-n auto`` wherever the suite runs whole."""
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"][
        "dev"
    ]
    assert any(each.startswith("pytest-xdist ") for each in dev)
    for name in ("test", "integration", "newest", "oldest", "docs"):
        commands = [
            step["run"]
            for step in jobs()[name]["steps"]
            if "pytest" in step.get("run", "")
        ]
        assert commands, name
        assert all("pytest -n auto" in command for command in commands), name


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
    assert summarise(tmp_path, "a", "--lived-fails").returncode == 2
    assert summarise(tmp_path).returncode == 2  # No shard.


def test_a_changed_functions_summary_fails_on_a_mutant_that_lived(
    tmp_path: Path,
) -> None:
    """``--lived-fails``: complete, but a mutant lived, is a failure too."""
    root = tmp_path / "mutants" / "src" / "domainkit"
    meta(root, {"domainkit.a.x_f__mutmut_1": 1, "domainkit.a.x_f__mutmut_2": 0})

    lived = summarise(tmp_path, "changed", "--lived-fails", "domainkit.a.*")
    weekly = summarise(tmp_path, "changed", "domainkit.a.*")
    meta(root, {"domainkit.a.x_f__mutmut_1": 1, "domainkit.a.x_f__mutmut_2": 36})
    caught = summarise(tmp_path, "changed", "--lived-fails", "domainkit.a.*")

    assert (lived.returncode, weekly.returncode, caught.returncode) == (1, 0, 0)
    assert "1 mutants no test caught" in lived.stdout
    assert "domainkit.a.x_f__mutmut_2: survived" in lived.stdout
    assert json.loads((tmp_path / "mutation-summary.json").read_text())["score"] == 100


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


def test_the_newest_job_audits_what_pip_resolves() -> None:
    """pip-audit over what ``pip install domainkit[...]`` resolves, weekly too."""
    newest = [step.get("run", "") for step in jobs()["newest"]["steps"]]
    assert any(".newest/bin/python -m pip_audit" in each for each in newest)


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


def test_make_cov_combines_both_runs() -> None:
    """``make cov`` reads the file ``make test`` writes, and the integration run's.

    ``coverage combine`` writes ``.coverage`` and skips it as an input, so a
    run that wrote ``.coverage`` itself would be dropped from the figure.
    """
    (written,) = re.findall(r"COVERAGE_FILE=(\S+)", " ".join(make_target("test")))
    inputs = make_target("cov")[0].split("--keep", 1)[1].split()

    assert inputs == [written, ".coverage.integration"]
    assert ".coverage" not in inputs


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


def test_a_change_mutates_only_the_functions_it_touched(tmp_path: Path) -> None:
    """Functions and methods whose lines changed, by mutmut's names; no others.

    A decorator's line counts as its function's; a class body's, a
    constant's, or a deleted function's lines name nothing.
    """
    source = tmp_path / "src" / "domainkit" / "shop"
    source.mkdir(parents=True)
    module = source / "orders.py"
    module.write_text(ORDERS, encoding="utf-8")
    (source / "__init__.py").write_text("def kept():\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "notes.txt").write_text("before\n", encoding="utf-8")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    changed = (
        ORDERS.replace("return 1", "return 2")  # total, in a function
        .replace("    @staticmethod\n", "    @classmethod\n")  # Order.make's decorator
        .replace('LIMIT = "a"', 'LIMIT = "b"')  # a constant
        .replace("        self.n = n", "        self.n = n + 0")  # Order.__init__
        .replace("def gone():\n    pass\n", "")  # deleted
    )
    module.write_text(changed, encoding="utf-8")
    (tmp_path / "src" / "notes.txt").write_text("after\n", encoding="utf-8")
    (source / "new.py").write_text("async def fresh():\n    pass\n", encoding="utf-8")
    git(tmp_path, "add", ".")

    done = subprocess.run(  # noqa: S603 - this interpreter and the script.
        [sys.executable, str(CHANGED_MUTANTS), "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    patterns = done.stdout.split()
    assert patterns == [
        "domainkit.shop.new.x_fresh__mutmut_*",
        "domainkit.shop.orders.x_total__mutmut_*",
        "domainkit.shop.orders.xǁOrderǁ__init____mutmut_*",
        "domainkit.shop.orders.xǁOrderǁmake__mutmut_*",
    ]
    assert (tmp_path / "changed-mutants.txt").read_text(encoding="utf-8") == "".join(
        f"{pattern}\n" for pattern in patterns
    )
    assert fnmatchcase("domainkit.shop.orders.x_total__mutmut_3", patterns[1])
    nothing = subprocess.run(  # noqa: S603 - this interpreter and the script.
        [sys.executable, str(CHANGED_MUTANTS), "HEAD"],
        cwd=tmp_path / "src",  # No src/ below: nothing changed there.
        capture_output=True,
        text=True,
        check=False,
    )
    assert (nothing.returncode, nothing.stdout) == (0, "\n")
    usage = subprocess.run(  # noqa: S603 - this interpreter and the script.
        [sys.executable, str(CHANGED_MUTANTS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert usage.returncode == 2
    assert "Usage:" in usage.stderr


ORDERS = '''"""Orders."""

LIMIT = "a"


def total():
    return 1


def gone():
    pass


class Order:
    kind = "order"

    def __init__(self, n):
        self.n = n

    @staticmethod
    def make():
        return Order(1)

    def untouched(self):
        return self.n
'''


def test_the_pull_request_mutates_what_it_changed_within_its_time() -> None:
    """``mutation (changed)``: the changed functions' mutants, reported, advisory.

    It runs on pull requests that change code only, compares the merge
    commit with its base (``HEAD^1``), stops mutmut in time to report, and
    is not a check the ruleset requires (ADR 0106).
    """
    job = jobs()["mutation-changed"]
    assert job["name"] == "mutation (changed)"
    assert "mutation-changed" in ADVISORY
    checkout = job["steps"][0]
    assert checkout["with"] == {"persist-credentials": False, "fetch-depth": 2}
    steps = {step.get("name", step.get("uses", "")): step for step in job["steps"]}
    found = steps["Find the changed functions"]["run"]
    assert ".github/scripts/changed_mutants.py HEAD^1" in found
    run = steps["Run their mutants"]["run"]
    match = re.search(r"timeout --signal=INT --kill-after=(\d+)m (\d+)m ", run)
    assert match, run
    kill_after, budget = int(match.group(1)), int(match.group(2))
    assert budget + kill_after + 15 <= job["timeout-minutes"]
    summary = steps["Summarise the changed functions' mutants"]
    assert ".github/scripts/mutation_summary.py changed" in summary["run"]
    assert "GITHUB_STEP_SUMMARY" in summary["run"]
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]
    assert config["mutmut"]["pytest_add_cli_args"] == ["-m", "not reads_own_source"]
    assert any(
        marker.startswith("reads_own_source:")
        for marker in config["pytest"]["ini_options"]["markers"]
    )
