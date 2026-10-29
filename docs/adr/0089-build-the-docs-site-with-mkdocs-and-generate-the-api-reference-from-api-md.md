# 0089. Build the docs site with MkDocs, and generate the API reference from api.md

- Status: Accepted; amended 2026-10-29 (at the end) by [0114](0114-split-the-reference-and-the-public-api-into-pages-by-area.md)
- Date: 2026-10-26

## Context

The documentation was Markdown read on GitHub: no index, no search, and
no reference generated from the code, although the docstrings already
carry it (DOCS-03, DOCS-05 of the 0.9.0 audit). `docs/api.md` lists every
public name ([0076](0076-public-api-is-docs-api-md.md)), one line each.
A site must not slow a pull request's checks, must catch broken links,
and must keep `test_docs.py`'s own link checks meaningful.

Measured on this repository (the whole site; one machine, shared):

| Tool | Build | Output | What it needs |
| --- | --- | --- | --- |
| MkDocs 1.6 with Material 9.8 and mkdocstrings-python 2.0 | 15.4 s (4.7 s without the API reference) | 204 pages, 16 MB | the docs group only: griffe reads the source, imports nothing |
| Zensical 0.0.72, the same `mkdocs.yml` | 16.0 s (5.1 s without) | 204 pages | committed stub pages: it runs no MkDocs hooks |
| pdoc 16, the API alone | 19.6 s | 98 pages, 27 MB | every extra installed: it imports every module; no narrative pages |

All three found the same broken links, and MkDocs and Zensical both
refuse a link to a missing heading. Material's own build warns that it
reaches its end of life in May 2027 and names Zensical, which builds this
`mkdocs.yml`, as its successor.

## Decision

- **The site** is built by MkDocs with Material, from `mkdocs.yml`, with
  `--strict` and every `validation` check set to warn, so a link to a
  missing page or heading fails the build. The tools are a `docs`
  dependency group; nothing is added at run time.
- **The API reference** is generated at build time by a hook
  (`docs/hooks/site.py`): one page per module `docs/api.md` lists,
  rendering with mkdocstrings exactly the names listed there. Nothing is
  committed: `docs/api.md` stays the contract and the docstrings the text,
  and a name added to both appears on the site without another change.
- **CI** builds the site in the `docs-site` job, a required check that
  runs beside the tests and installs only the docs group; the built site
  is an artifact. `test_docs.py`'s link checks stay: they also read the
  README, the CHANGELOG, and the pages outside `docs/`, as GitHub shows
  them.

## Consequences

- A newcomer gets an index, search, and the getting-started path; a user
  gets every public name with its docstring, and a broken docstring
  reference fails CI.
- Publishing the site (GitHub Pages, versioned per minor) is a later step:
  it needs a job with `pages: write`, and a choice of hosting.
- Moving to Zensical before Material's end of life means generating the
  API pages before the build instead of in a hook; the rest of
  `mkdocs.yml` carries over.

## Amendment (2026-10-29, 0.13): the hook reads the public API's pages

The public API is listed on `docs/api/`'s pages, which `docs/api.md` links
([0114](0114-split-the-reference-and-the-public-api-into-pages-by-area.md)).
The hook reads every one of them and renders a page per module they list,
in the order of the modules' names, as before; the generated pages and
their URLs are unchanged.
