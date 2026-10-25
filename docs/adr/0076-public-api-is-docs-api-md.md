# 0076. The public API is what `docs/api.md` lists, which is every `__all__`

- Status: Accepted
- Date: 2026-10-25

## Context

`docs/versioning.md` defined the public API as the names a package's
`__init__` exports. The pages said otherwise: of 125 dotted names they
told users to import, 62 were outside that rule (the 0.9.0 design audit,
DSGN-05), among them every middleware class a project names in
`MIDDLEWARE`, the ASGI `Application`, `expect_version`, the mail backends,
and the clock. By the written rule those could change in any release; in
practice the maintainers could not change them either. Only 29 modules
had an `__all__`.

Two rules were considered: "public is whatever a page mentions", which
cannot be checked mechanically and drifts with every edit, and "public is
`__all__`", which Python tools already read (`import *`, type checkers'
re-export rules, IDEs) and a test can hold to a written list.

## Decision

- A name is public when a package or module lists it in `__all__`. Every
  module whose names the pages use has one, holding those names and the
  types their signatures need; nothing more.
- `docs/api.md` lists every such name under its module, one line each, in
  `__all__`'s order. `test_api.py` fails when the page and the code differ
  (a section missing or extra, a name missing, extra, or not importable),
  and when another page writes `domainkit.x.Name`, or imports a name in an
  example, that is not listed.
- `docs/versioning.md` states the rule. A patch release may add names but
  deprecates none; deprecations wait for the next minor release, listed in
  `docs/versioning.md` until then.

## Consequences

- Users read what they may rely on in one place; the maintainers know
  what they may change, and CI tells them when a change touches it.
- Adding `__all__` to a module narrows `from module import *` to the
  public names; no code in the repository or its docs does that.
- The list is long (about 700 names in 96 modules). 1.0 freezes it after
  0.10's structural changes (DSGN-01, DSGN-02) have moved names to their
  final modules.
