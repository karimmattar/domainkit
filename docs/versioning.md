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

## Making a release

1. Move the changelog's **Unreleased** entries under the new version and date.
2. Update `version` in `pyproject.toml`.
3. Merge to `main` once CI passes.
4. Tag that commit `vX.Y.Z` and push the tag. `.github/workflows/release.yml`
   then:
   - checks that the tag matches the version and is on a commit of `main`,
     so only reviewed changes are released;
   - runs the whole CI workflow on that commit: lint, types, tests on every
     Python, integration against real servers, coverage, the wheel's smoke
     test, pip-audit, and gitleaks;
   - waits for a reviewer to approve the `pypi` environment;
   - publishes the wheel and sdist that CI built to PyPI, with attestations.

One-time setup, before the first release:

- On PyPI, add a trusted publisher for the `domainkit` project: this
  repository, workflow `release.yml`, environment `pypi`.
- On GitHub, create the `pypi` environment and require a reviewer for it,
  so a pushed tag cannot publish without approval. Limit its deployments to
  `v*` tags. The workflow cannot check these settings: without them, a tag
  that passes CI publishes at once.
- Add a tag ruleset for `v*`, so only maintainers can create, move, or
  delete release tags.
- Protect `main` with a branch ruleset: changes through pull requests, with
  the `lint`, `test (3.13)`, `test (3.14)`, `integration`, `coverage`,
  `build`, and `audit` checks passing.
  Require code owner review once there is a second maintainer: GitHub does
  not let authors approve their own pull requests, so a sole maintainer
  would bypass the ruleset on every merge, skipping the checks too.
- Turn on private vulnerability reporting, which `SECURITY.md` asks
  reporters to use.
