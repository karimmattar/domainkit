# 0114. Split the reference and the public API into pages by area

- Status: Accepted
- Date: 2026-10-29
- Amends: [0076](0076-public-api-is-docs-api-md.md), [0089](0089-build-the-docs-site-with-mkdocs-and-generate-the-api-reference-from-api-md.md)

## Context

The 0.9.0 audit found pages too large to read as references (DOCS-05).
By 0.12.0 the two that a reader looks things up in had grown most:

| Page | Lines | Headings | What a reader looks up |
| --- | --- | --- | --- |
| `docs/reference.md` | 1,303 | 33, under 10 sections | a setting, a check id, a middleware's place, an error code, a command |
| `docs/api.md` | 1,377 | 110, one per module | a module's public names |

The guides that come next (`runtime.md`, 1,088 lines; `performance.md`,
915; `databases.md`, 743) are read by topic, each section a how-to with
its own links in, and `performance.md` is mostly the record of what each
release measured; they are not looked up row by row, and stay as they are.

Three layouts were considered:

- **One page each, with a table of contents.** The site shows one, and
  GitHub its outline, but a reader still scrolls a page of 1,300 lines,
  and every release adds rows to it.
- **Pages by area, with redirects from the old anchors**
  (`mkdocs-redirects`). It is a new docs dependency (`test_ci.py` holds the
  group to two), it redirects pages on the site only, not anchors, and the
  site is not published yet, so no old site URL exists to keep. On GitHub
  nothing can redirect an anchor.
- **Pages by area, the old page an index of them.** Each section keeps its
  name and its anchor on its new page; a link from before, which GitHub
  opens at the index's top, finds the section there by name.

## Decision

- **The reference** is `docs/reference/`, a page for each area a reader
  looks a thing up by: settings, databases and the other backends, HTTP
  (settings, middleware, live connections), server limits, deployment
  checks, errors and responses, signing, observability, the test plugin,
  commands, and extension points. `docs/reference.md` lists the pages and
  what each holds. The settings tables span three of them; `test_docs.py`
  reads the tables, and the names documented, across every page of
  `docs/reference/`, and the commands, the middleware, and the server
  command from their own pages.
- **The public API** is listed on `docs/api/`, a page for each part of the
  package: setup, settings, and the process; domains, data, and
  resources; HTTP; users and permissions; the other optional apps.
  `docs/api.md` keeps the policy and links each page. A module is listed
  on exactly one page, and the index lists none; `test_api.py` and the
  site's hook read every page, so the rule of 0076 holds unchanged: the
  pages together list every `__all__`, and nothing else.
- **Links** go to the section's new page, not to the index: those of the
  docs, the README, and the code. The code names pages as text
  (`(docs/reference/servers.md, "Server limits")`), which no check read;
  `test_docs.py` now fails when such a page, its anchor, or the heading it
  quotes is missing.

## Consequences

- The longest reference page is 239 lines, and the longest API page 372
  (the optional apps, 30 modules); a release adds rows to the page of the
  area it changes.
- A change adds its setting, check id, or command to the page of its area,
  and a public name to its package's page. A new setting's row or a
  module's section put on `reference.md` or `api.md` instead fails the
  tests: they read the settings on `docs/reference/` alone, and `api.md`
  may list no module.
- Links from before 0.13 into `reference.md#<section>`, as in the README
  0.12.0 published on PyPI, open the index, not the section. The
  changelog names sections as text (`docs/reference.md`, "Server
  limits"), and the index lists each by that name.
- Moving a section again means moving its links again; the link checks
  (`mkdocs build --strict`, `test_docs.py`) find every one left behind.
