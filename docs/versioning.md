# Versions, compatibility, and deprecations

## Version numbers

domainkit follows [Semantic Versioning](https://semver.org/). Before 1.0, the
minor number plays the role of the major one:

| Release | May contain |
| --- | --- |
| `0.x.Y` (patch) | Bug and security fixes only. Always safe to take. |
| `0.X.0` (minor) | New features; removals announced at least one minor release earlier. |
| `1.0.0` and later | Removals only in a major release. |

Projects pin a compatible range: from 1.0, up to the next major
(`domainkit = ">=1.0,<2"`, which `domainkit init` writes); before it, up to
the next minor (`">=0.12,<0.13"`), upgrading one minor release at a time.

## What is public

These are covered by the policy:

- the names listed in [api.md](api.md): every name a package or module
  puts in its `__all__` (`domainkit.ddd.Selection`,
  `domainkit.http.middleware.CORSMiddleware`, ...), and only those. A test
  keeps the page and the code in step, and fails when another page tells
  users to import a name that is not listed
- documented settings and their defaults
- management commands and their options
- the generated project and domain layout (`init`, `startdomain`), which
  imports public names only
- the extras (`pip install "domainkit[keys]"`) and what each makes work
- check ids, and the problem codes [reference/problems.md](reference/problems.md) lists
- table schemas of the contrib apps, which change only with a migration in the release
- the package's extras (`domainkit[postgres]`, `domainkit[jwt]`, ...) and the
  packages each installs, and the `domainkit` command. Removing or renaming
  an extra breaks a project silently: pip only warns about an extra it does
  not know, and installs without its packages, so the project fails at
  import, in production. An extra is deprecated like a name: kept, and
  announced, until the next major release (INFRA-19)

Names starting with `_`, modules without `__all__`, names a module does not
list in its `__all__`, and the exact wording of log and error messages are
internal and may change in any release. Until 0.9.2 the rule named only
what a package's `__init__` exported, which left out most of what the pages
told users to use, such as the middleware; those are listed now
([ADR 0076](adr/0076-public-api-is-docs-api-md.md)).

## Deprecating

A public API is never removed without warning:

1. The release that deprecates it keeps it working and warns with
   `domainkit.deprecation.DomainkitDeprecationWarning`, naming the
   replacement and the release that removes it:

   ```python
   @warnings.deprecated(
       "Use Page.items; removed in domainkit 0.4.",
       category=DomainkitDeprecationWarning,
   )
   ```

2. The changelog lists it under **Deprecated**, with the replacement.
3. It is removed no earlier than the next minor release (the next major
   release after 1.0), and the changelog lists it under **Removed**.

A security fix may break compatibility in a patch release when there is no
safe alternative. Its changelog entry says so.

A patch release deprecates nothing: a new warning would fail the projects
that make deprecations errors, as below. When a patch adds a better name,
the old one stays without a warning, and the next minor release deprecates
it.

### Deprecations planned for 0.11

All done: 0.11 removed the names and the setting that 0.10 deprecated,
refuses `crud=True` without the fields it writes, and makes `"refuse"` the
default of `SCOPED_CREDENTIALS` and `IDEMPOTENCY.ANONYMOUS`.
[upgrading.md](upgrading.md#010-to-011) lists each, with its replacement
and the setting that keeps a default's old behaviour.

### Deprecations planned for 0.13

All done: 0.13 removed what 0.12 deprecated.
[upgrading.md](upgrading.md#012-to-013) lists each, with what to write
instead.

| Deprecated in 0.12 | Use instead | What 0.13 did |
| --- | --- | --- |
| The hook methods of `DomainConfig` (`startup`, `shutdown`, `check`, `seed`, `erase_subject`, `export_subject`, `check_application`, `deployment_warnings`, `stage`, `committed`, `stage_transaction`, `transaction_committed`, `migrated`), overridden | The points `on_<method>` of `domainkit.apps.hooks` and `domainkit.db.hooks`, registered in `ready()` | Removed them; `setup()` refuses a configuration that still defines one (`apps.E002`), until 2.0 |
| `domainkit.ddd.preconditions.expect_version` | `UnitOfWork.expect(model, id, versions)` | Removed it, and the context-wide expectation |

### Deprecations planned for 2.0

1.0 renamed these names of 0.12, and froze the rest ([ADR 0125](adr/0125-freeze-the-public-api-for-1-0-renaming-what-was-never-published.md)).
Each old name still works through 1.x, warning with
`DomainkitDeprecationWarning`; 2.0 removes it, with `apps.E002`.
[upgrading.md](upgrading.md#013-to-014) says what to write instead.

| Deprecated in 1.0 | Use instead |
| --- | --- |
| `on_check` of `domainkit.apps.hooks` | `domainkit.apps.hooks.on_ready` |
| `DomainConfig.RETENTION` | `DomainConfig.retention` |
| `MESSAGING.SHUTDOWN_TIMEOUT` | `MESSAGING.DRAIN_TIMEOUT` |
| `domainkit.contrib.messaging.dbclock` | `domainkit.db.dbclock` |
| `contrib.contenttypes`'s `DEFAULT_PERMISSIONS`, `InstalledModel`, `ModelOptions`, `installed_models`, `model_options`, `content_type_key` | The same names of `domainkit.db.models` (`model_key` for `content_type_key`) |
| `contrib.webhooks.WebhookSettings` | `WebhooksSettings` |
| `Credentials.session` | `Credentials.login` |

Names that left `__all__` in 1.0 (internals no page used) still import,
unchanged and without a warning, until 2.0.

To find deprecated uses before upgrading, make the warning an error in the
project's pytest settings:

```toml
[tool.pytest.ini_options]
filterwarnings = ["error::domainkit.deprecation.DomainkitDeprecationWarning"]
```

## Security fixes and support

[SECURITY.md](../SECURITY.md) says how to report a vulnerability and how
fast each step is answered. From 1.0, these release lines get fixes
(INFRA-18):

| Release line | Gets | For how long |
| --- | --- | --- |
| The latest minor of the current major (`1.X`, until 2.0 is released) | Every fix, bugs and vulnerabilities of every severity, as patch releases (`1.X.Y`) | Until the next minor is released |
| The last 1.x minor, once 2.0 is released | Fixes for critical and high severity vulnerabilities, as patch releases | 12 months from the release of 2.0.0 |
| Every other 1.x minor | Nothing: upgrade to the latest minor, which breaks nothing | |
| Every 0.x release | Nothing: upgrade to 1.x ([upgrading.md](upgrading.md)) | |

- The severity is the one `SECURITY.md`'s steps confirm; each fix is
  published as a GitHub security advisory naming the patch releases that
  carry it.
- A fix that cannot be backported to the last 1.x minor without a breaking
  change is not: the advisory says so, and says what to upgrade to.
- Only the newest patch release of a supported line is supported:
  upgrade within a line as soon as a patch is out.
- The Python versions a release supports are the ones its
  `requires-python` admits, every one of which CI tests (below). A Python
  version its maintainers stop fixing is dropped in the next minor release.
- 2.0's own support statement says what its last minor gets after 3.0.
- Fixes for the last 1.x minor after 2.0 are made on a `1.x` maintenance
  branch; `release.yml` and the rulesets, which release only commits of
  `main`, gain that branch when 2.0 is released.

## Supported Python and dependencies

Each release supports the Python versions in `requires-python` (currently
3.13 and later) and declares compatible ranges for FastAPI, Pydantic,
SQLAlchemy, Alembic, and the database drivers. Raising a minimum version is
a minor-release change and appears in the changelog. CI tests each Python
release `requires-python` admits: `test (3.13)`, `test (3.14)` and
`test (3.15)` run the suite on the locked set, and the classifiers name the
same three; a new Python is added to the matrix, the ruleset and the
classifiers once it is released, or `requires-python` stops before it
(INFRA-17). Free-threaded 3.14t runs the tests of the state threads share
(`test (3.14t)`), without the `immudb` extra, whose grpcio and protobuf
ship no wheels for it.

The ranges follow one rule (ADR 0075): from the oldest version CI tests up
to the dependency's next major version, `<1.0.0` for one still at 0.x.
SQLAlchemy stops before its next minor (`<2.2`), since its second number
marks API changes; `tzdata`, being data, has no top. Nothing is pinned
with `==`. The lock file pins the set CI's jobs test, the wheel's smoke
test in `build` included; the `newest` job installs the wheel with the
newest releases the ranges allow and runs the suite, on the newest Python,
and the `oldest` job does the same at every range's floor, on the oldest,
on every pull request and weekly. Both run pip-audit over what they
installed: a floor is past every advisory known for it, and one an advisory
overtakes is raised (INFRA-15). When an upstream release breaks the newest, the
next patch release fixes domainkit or lowers that range's top.
Renovate widens a range when a new major appears, once CI passes with it.

Each release supports PostgreSQL 16 to 18 (TEST-18). CI runs the
suite against the newest (`integration`, on 18) and the oldest
(`postgresql (16)`); both are required checks. Dropping a version is a
minor-release change and appears in the changelog; a new major is added
once both jobs pass with it. SQLite is for development and tests.

Every package the code imports is declared, even one that another
dependency already installs (anyio through Starlette, pydantic-core through
pydantic): in the dependencies when the core imports it, otherwise in the
extra of the feature that does (`aiormq` in `rabbitmq`, `httpcore2` in
`httpclient`, `grpcio`, `ecdsa` and `protobuf` in `immudb`). CI's lint job
checks it with deptry (`[tool.deptry]` in `pyproject.toml`).

## Making a release

1. Move the changelog's **Unreleased** entries under the new version and
   date (`## [X.Y.Z] - YYYY-MM-DD`), leaving **Unreleased** empty.
2. Update `version` in `pyproject.toml`. Steps 1 and 2 make the release
   commit; the tag goes on it, and on no other.
3. Merge to `main` once CI passes. The push to `main` runs the whole CI
   workflow on the merged commit: lint, types, tests on every Python,
   integration against real servers, coverage, the benchmark ladder, the
   wheel's smoke test and bill of materials, Semgrep, pip-audit, and
   gitleaks. Its build job points the README's and the project URLs'
   links to this repository at the tag `vX.Y.Z` before building, so the
   PyPI page links to the documents of the release, not to `main`
   (INFRA-22). That run then attests the wheel and sdist it built with SLSA
   build provenance naming it, and the wheel's CycloneDX bill of
   materials, signed through GitHub (check one with `gh attestation
   verify domainkit-X.Y.Z-py3-none-any.whl --repo karimmattar/domainkit
   --signer-workflow karimmattar/domainkit/.github/workflows/ci.yml`).
4. Tag that commit `vX.Y.Z` and push the tag. `.github/workflows/release.yml`
   then:
   - checks that the tag matches the version and is on a commit of `main`,
     so only changes that passed CI in a pull request are released;
   - checks that the commit is the release (`.github/scripts/release_check.py`,
     INFRA-16): the version is `X.Y.Z` or a pre-release (`aN`, `bN`, `rcN`);
     the changelog's newest section is the version's, dated, and
     **Unreleased** is empty; the version changed in that commit, or its
     changelog section was added in it; and the version is newer than
     every tag of its line (`X.Y`), and a new line's first release newer
     than every tag;
   - finds the CI run of that commit's push to `main`, waits for it if it
     is still running, and stops unless each check the `main` ruleset
     requires, and `attest`, succeeded in it (`.github/scripts/required_jobs.py`).
     The run's own conclusion is not used: it counts the advisory jobs,
     and `stress (3.14t)` crashes now and then inside SQLAlchemy on 3.14t
     (INFRA-14). Nothing is run twice
     (ADR [0007](adr/0007-own-repository-released-to-pypi.md#amendment-2026-10-26-010-a-release-reuses-mains-ci-run));
   - runs pip-audit and gitleaks again, as an advisory can appear after
     that run, and pip-audit over what the published ranges resolve to
     today, every extra included (INFRA-21);
   - checks that run's attestations of every file it publishes: signed by
     `ci.yml` on `main` at the tagged commit, on GitHub's runners;
   - waits for a reviewer to approve the `pypi` environment;
   - publishes the wheel and sdist that CI built to PyPI, with attestations.

   Before approving, the maintainer creates the GitHub Release for the
   tag by hand (Releases, Draft a new release), with the CHANGELOG's
   section for the version as its notes; the workflow makes none.

   CI keeps a run's `dist` and `sbom` for 90 days. To release an older
   commit, re-run its CI run (`gh run rerun <id>`) first. Each tag gets a
   release run of its own, never cancelled; push one tag per release.

One-time setup, before the first release:

- On PyPI, add a trusted publisher for the `domainkit` project: this
  repository, workflow `release.yml`, environment `pypi`.
- On GitHub, create the `pypi` environment and require a reviewer for it,
  so a pushed tag cannot publish without approval. Limit its deployments to
  `v*` tags. The workflow cannot check these settings: without them, a tag
  that passes CI publishes at once.
- Import the three rulesets in `.github/rulesets/` (Settings, Rules,
  Rulesets, New ruleset, Import a ruleset), and delete the ones they
  replace. With the GitHub CLI, `gh api --method POST
  repos/OWNER/REPO/rulesets --input .github/rulesets/main.json` creates
  one, and `--method PUT repos/OWNER/REPO/rulesets/ID` replaces one.
  Import `main.json` again whenever a change edits it (a check added,
  renamed, or removed): the release judges main's CI run by the
  `main.json` of the tagged commit, since its token cannot read the live
  ruleset, so a ruleset left behind gates pull requests on other checks
  than the release requires. `test_ci.py`, in the required test jobs,
  checks that the file names every check CI gates a push on, and that a
  run with two jobs of a required check's name is refused.
  - `main.json`: changes reach `main` only as squash-merged pull requests,
    up to date with it, with these checks from GitHub Actions passing:
    `changes`, `docs`, `lint`, `test (3.13)`, `test (3.14)`, `test (3.15)`,
    `test (3.14t)`, `integration`, `postgresql (16)`, `bench`, `coverage`,
    `build`, `sbom`, `semgrep`, `docs-site`, `audit`, and `codeql` (from
    `codeql.yml`), and with no new CodeQL alert of high or critical
    security severity, or of error severity (its code scanning rule,
    INFRA-21). A pull request that changes only `docs/**` and the Markdown
    at the top of the repository runs `docs` (the tests that read the
    docs), `docs-site`, `lint`, `semgrep`, `audit` and `codeql`; the others
    are skipped, which reports success, and the test matrix skips its
    steps. Any other pull request runs them all and skips `docs`. Nobody
    pushes to `main` or rewrites it; an admin may bypass the rules only
    when merging a pull request, a choice GitHub records. The `newest` and
    `oldest` jobs run on pull requests too but are not required: what
    upstream releases is not the change's doing. Neither is `stress
    (3.14t)`, which runs the free-threading tests 30 times on 3.14t: on
    3.14.6t the tests that make engines on several threads at once (marked
    `engines_on_threads`) crash the interpreter in some runs, inside
    SQLAlchemy's compiled extensions, at 0.9.2 too; `test (3.14t)` leaves
    those tests out, and a release ignores `stress (3.14t)` (INFRA-14).
    Nor is `mutation (changed)` (0.12), which runs the mutants of the
    functions a pull request changes, and fails when one lives: an
    equivalent mutant can live, which the author explains rather than
    kills ([ADR 0106](adr/0106-mutate-what-a-pull-request-changes.md)), nor
    the shards it hands a large change's mutants to, `mutation (changed,
    N)`, and `mutation (changed, summary)`, which judges them (0.12.1).
    `domainkit_tests/test_ci.py` checks that the list is every other job a
    pull request runs, and is this one.
  - `release-tags.json`: nobody, admins included, moves or deletes a `v*`
    tag.
  - `release-tag-creation.json`: only admins create `v*` tags.
- The `main` ruleset requires no approving review while there is one
  maintainer: GitHub does not let authors approve their own pull requests,
  so requiring one would make every merge a bypass, skipping the checks
  too. With a second maintainer, set `required_approving_review_count` to
  1 and `require_code_owner_review` and `require_last_push_approval` to
  `true` in `main.json`, and import it again.
- Renovate proposes dependency updates (`renovate.json`). Turn off
  Dependabot's security update pull requests (Settings, Advanced
  Security), keeping its alerts and the dependency graph, so that one bot
  proposes changes.
- Turn on private vulnerability reporting, which `SECURITY.md` asks
  reporters to use.
