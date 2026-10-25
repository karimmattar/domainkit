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

- names exported by a package's `__init__` (`domainkit.ddd`, `domainkit.http`, `domainkit.contrib.messaging`, ...)
- documented settings and their defaults
- management commands and their options
- the generated domain layout (`startdomain`)
- table schemas of the contrib apps, which change only with a migration in the release

Names starting with `_`, modules not exported from a package, and the exact
wording of log messages are internal and may change in any release.

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

To find deprecated uses before upgrading, make the warning an error in the
project's pytest settings:

```toml
[tool.pytest.ini_options]
filterwarnings = ["error::domainkit.deprecation.DomainkitDeprecationWarning"]
```

## Supported Python and dependencies

Each release supports the Python versions in `requires-python` (currently
3.13 and later) and declares compatible ranges for FastAPI, Pydantic,
SQLAlchemy, Alembic, and the database drivers. Raising a minimum version is
a minor-release change and appears in the changelog.

The ranges follow one rule (ADR 0075): from the oldest version CI tests up
to the dependency's next major version, `<1.0.0` for one still at 0.x.
SQLAlchemy stops before its next minor (`<2.2`), since its second number
marks API changes; `tzdata`, being data, has no top. Nothing is pinned
with `==`. The lock file pins the set CI's jobs test, and the `newest` job
installs the wheel with the newest releases the ranges allow and runs the
suite, on every pull request and weekly. When an upstream release breaks
it, the next patch release fixes domainkit or lowers that range's top.
Renovate widens a range when a new major appears, once CI passes with it.

## Making a release

1. Move the changelog's **Unreleased** entries under the new version and date.
2. Update `version` in `pyproject.toml`.
3. Merge to `main` once CI passes.
4. Tag that commit `vX.Y.Z` and push the tag. `.github/workflows/release.yml`
   then:
   - checks that the tag matches the version and is on a commit of `main`,
     so only changes that passed CI in a pull request are released;
   - runs the whole CI workflow on that commit: lint, types, tests on every
     Python, integration against real servers, coverage, the benchmark
     ladder, the wheel's smoke test and bill of materials, Semgrep,
     pip-audit, and gitleaks;
   - attests the wheel and sdist with SLSA build provenance, and the wheel's
     CycloneDX bill of materials, signed through GitHub (check one with
     `gh attestation verify domainkit-X.Y.Z-py3-none-any.whl --repo
     karimmattar/domainkit`);
   - waits for a reviewer to approve the `pypi` environment;
   - publishes the wheel and sdist that CI built to PyPI, with attestations.

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
    `lint`, `test (3.13)`, `test (3.14)`, `test (3.14t)`, `integration`,
    `bench`, `coverage`, `build`, `sbom`, `semgrep`, and `audit`. Nobody
    pushes to `main` or rewrites it; an admin may bypass the rules only
    when merging a pull request, a choice GitHub records. The `newest` job
    runs on pull requests too but is not required: an upstream release is
    not the change's doing. `domainkit_tests/test_ci.py` checks that the
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
