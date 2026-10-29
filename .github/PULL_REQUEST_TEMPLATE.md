## What changes

<!-- What a project sees after this change, in a sentence or two. Name the
issue or audit id it answers, if any. -->

## Why

<!-- The problem, and why this is the fix: the options you weighed, and
what decided between them (a measurement, a proof of concept). -->

## How it was tested

<!-- The tests that show it, and that they fail without the change. For a
request path, the benchmark ladder before and after. -->

## Checklist

- [ ] `make check` passes (lint, types, tests, docs), and `make integration` if it touches a server
- [ ] New and changed code is covered, branches included, and mutants of the changed functions are killed or explained
- [ ] An entry under `## [Unreleased]` in `CHANGELOG.md`; a **Breaking:** one is in `docs/upgrading.md` too
- [ ] Public names in `__all__` and on their page of `docs/api/`, with docstrings; a renamed or moved name keeps working, deprecated, for a minor
- [ ] The page in `docs/` that describes the change, and its page of `docs/reference/` for a new setting, check, or command
- [ ] An ADR for a decision that is costly to reverse ([CONTRIBUTING.md](../CONTRIBUTING.md#writing-it-up))
