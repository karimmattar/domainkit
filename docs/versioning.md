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

### Deprecations planned for 0.10

| Old name | Use instead | Since |
| --- | --- | --- |
| `domainkit.ddd.query.Query` | `domainkit.ddd.Selection` | 0.9.2 |
| `domainkit.contrib.evidence.model` | `domainkit.contrib.evidence` (or its `models`) | 0.9.2 |

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
