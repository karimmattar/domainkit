# Versions, compatibility, and deprecations

## Version numbers

domainkit follows [Semantic Versioning](https://semver.org/). Before 1.0, the
minor number plays the role of the major one:

| Release | May contain |
| --- | --- |
| `0.x.Y` (patch) | Bug and security fixes only. Always safe to take. |
| `0.X.0` (minor) | New features; removals announced at least one minor release earlier. |
| `1.0.0` and later | Removals only in a major release. |

Projects pin a compatible range, for example `domainkit = ">=0.2,<0.3"`, and
upgrade one minor release at a time.

## What is public

These are covered by the policy:

- the names listed in [api.md](api.md): every name a package or module
  puts in its `__all__` (`domainkit.ddd.Selection`,
  `domainkit.http.middleware.CORSMiddleware`, ...), and only those. A test
  keeps the page and the code in step, and fails when another page tells
  users to import a name that is not listed
- documented settings and their defaults
- management commands and their options
- the generated domain layout (`startdomain`)
- table schemas of the contrib apps, which change only with a migration in the release

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
| The hook methods of `DomainConfig` (`startup`, `shutdown`, `check`, `seed`, `erase_subject`, `export_subject`, `check_application`, `deployment_warnings`, `stage`, `committed`, `stage_transaction`, `transaction_committed`, `migrated`), overridden | The points `on_<method>` of `domainkit.apps.hooks` and `domainkit.db.hooks`, registered in `ready()` | Removed them; `setup()` refuses a configuration that still defines one (`apps.E002`), until 1.0 |
| `domainkit.ddd.preconditions.expect_version` | `UnitOfWork.expect(model, id, versions)` | Removed it, and the context-wide expectation |

### Deprecations planned for 0.14

None yet.

To find deprecated uses before upgrading, make the warning an error in the
project's pytest settings:

```toml
[tool.pytest.ini_options]
filterwarnings = ["error::domainkit.deprecation.DomainkitDeprecationWarning"]
```

## Security fixes and support

**Draft, for the owner to confirm.** Until it is confirmed,
[SECURITY.md](../SECURITY.md) holds: only the latest minor release gets
fixes, and there are no backports before 1.0.

| Release line | Gets | For how long |
| --- | --- | --- |
| The latest minor (`0.X`) | Every fix, as patch releases (`0.X.Y`) | Until the next minor is released |
| The minor before it (`0.X-1`) | Fixes for critical and high severity vulnerabilities only, where the fix can be made without a breaking change | 90 days after the next minor is released |
| Older minors | Nothing | |

- The severity is the one `SECURITY.md`'s steps confirm; each fix is
  published as a GitHub security advisory naming the patch releases that
  carry it.
- A fix that cannot be backported safely is not: the advisory says so,
  and says what to upgrade to.
- Only the newest patch release of a supported line is supported:
  upgrade within a line as soon as a patch is out.
- Python versions follow `requires-python`; a Python version that its
  maintainers stop fixing is dropped in the next minor.
- 1.0 will state a longer support period for its majors; nothing here
  promises one before then.

## Supported Python and dependencies

Each release supports the Python versions in `requires-python` (currently
3.13 and later) and declares compatible ranges for FastAPI, Pydantic,
SQLAlchemy, Alembic, and the database drivers. Raising a minimum version is
a minor-release change and appears in the changelog.

The ranges follow one rule (ADR 0075): from the oldest version CI tests up
to the dependency's next major version, `<1.0.0` for one still at 0.x.
SQLAlchemy stops before its next minor (`<2.2`), since its second number
marks API changes; `tzdata`, being data, has no top. Nothing is pinned
with `==`. The lock file pins the set CI's jobs test; the `newest` job
installs the wheel with the newest releases the ranges allow and runs the
suite, and the `oldest` job does the same at every range's floor, on every
pull request and weekly. When an upstream release breaks the newest, the
next patch release fixes domainkit or lowers that range's top.
Renovate widens a range when a new major appears, once CI passes with it.

Every package the code imports is declared, even one that another
dependency already installs (anyio through Starlette, pydantic-core through
pydantic): in the dependencies when the core imports it, otherwise in the
extra of the feature that does (`aiormq` in `rabbitmq`, `httpcore2` in
`httpclient`, `grpcio`, `ecdsa` and `protobuf` in `immudb`). CI's lint job
checks it with deptry (`[tool.deptry]` in `pyproject.toml`).

## Making a release

1. Move the changelog's **Unreleased** entries under the new version and date.
2. Update `version` in `pyproject.toml`.
3. Merge to `main` once CI passes. The push to `main` runs the whole CI
   workflow on the merged commit: lint, types, tests on every Python,
   integration against real servers, coverage, the benchmark ladder, the
   wheel's smoke test and bill of materials, Semgrep, pip-audit, and
   gitleaks. That run then attests the wheel and sdist it built with SLSA
   build provenance naming it, and the wheel's CycloneDX bill of
   materials, signed through GitHub (check one with `gh attestation
   verify domainkit-X.Y.Z-py3-none-any.whl --repo karimmattar/domainkit
   --signer-workflow karimmattar/domainkit/.github/workflows/ci.yml`).
4. Tag that commit `vX.Y.Z` and push the tag. `.github/workflows/release.yml`
   then:
   - checks that the tag matches the version and is on a commit of `main`,
     so only changes that passed CI in a pull request are released;
   - finds the CI run of that commit's push to `main`, waits for it if it
     is still running, and stops unless it succeeded; nothing is run twice
     (ADR [0007](adr/0007-own-repository-released-to-pypi.md#amendment-2026-10-26-010-a-release-reuses-mains-ci-run));
   - runs pip-audit and gitleaks again, as an advisory can appear after
     that run;
   - checks that run's attestations of every file it publishes: signed by
     `ci.yml` on `main` at the tagged commit, on GitHub's runners;
   - waits for a reviewer to approve the `pypi` environment;
   - publishes the wheel and sdist that CI built to PyPI, with attestations.

   Before approving, the maintainer creates the GitHub Release for the
   tag by hand (Releases, Draft a new release), with the CHANGELOG's
   section for the version as its notes; the workflow makes none.

   CI keeps a run's `dist` and `sbom` for 90 days. To release an older
   commit, re-run its CI run (`gh run rerun <id>`) first.

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
  - `main.json`: changes reach `main` only as squash-merged pull requests,
    up to date with it, with these checks from GitHub Actions passing:
    `changes`, `docs`, `lint`, `test (3.13)`, `test (3.14)`, `test (3.14t)`,
    `integration`, `bench`, `coverage`, `build`, `sbom`, `semgrep`, `docs-site`,
    and `audit`. A pull request that changes only `docs/**` and the Markdown at
    the top of the repository runs `docs` (the tests that read the docs),
    `docs-site`, `lint`, `semgrep` and `audit`; the others are skipped, which reports
    success, and the test matrix skips its steps. Any other pull request
    runs them all and skips `docs`. Nobody
    pushes to `main` or rewrites it; an admin may bypass the rules only
    when merging a pull request, a choice GitHub records. The `newest` and
    `oldest` jobs run on pull requests too but are not required: what
    upstream releases is not the change's doing. Neither is `stress
    (3.14t)`, which runs the free-threading tests 30 times on 3.14t: on
    3.14.6t they crash the interpreter in some runs, inside SQLAlchemy's
    compiled extensions, at 0.9.2 too. Nor is `mutation (changed)` (0.12),
which runs the mutants of the functions a pull request changes, and fails
when one lives: an equivalent mutant can live, which the author explains
rather than kills ([ADR 0106](adr/0106-mutate-what-a-pull-request-changes.md)).
`domainkit_tests/test_ci.py` checks that the
    list is every other job a pull request runs, and is this one.
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
