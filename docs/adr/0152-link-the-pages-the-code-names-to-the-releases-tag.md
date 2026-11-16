# 0152. Link the pages the code names to the release's tag

- Status: Accepted
- Date: 2026-11-16

## Context

A project reported (D2) that the wheel holds no prose. Docstrings, error
messages, and `check`'s hints name pages as `docs/live.md` and decisions
as `ADR 0057`, and neither is in the package. The project read them in a
clone of the repository.

What the package names:

- About 70 page names in `src/`: in docstrings, in six messages and
  hints (`check`'s HTTPS note, `SECRET_KEY`'s, the key ring's, the
  ledger's), in `init`'s help and output, and, half of them, in the notes
  the templates write into a project, which has no `docs/` either.
- Nearly 200 citations of over 60 records.
- `test_docs.py` checked that each page named is there, with its anchor.
  Nothing checked that a record cited is there.

What already links into the documentation:

- CI's build job points the README's and the project URLs' links at the
  tag `vX.Y.Z` before it builds, so the PyPI page opens the release's
  pages (INFRA-22).
- `init` writes the Dockerfile's notes with `docs_url`, the documentation
  under the release's tag (user report S1).
- Nobody, admins included, moves or deletes a `v*` tag
  (`.github/rulesets/release-tags.json`). A link under a tag shows that
  release's text for good, as a link under a commit does [11].
- GitHub renders the Markdown at a tag, with its anchors and its links
  between pages, those to files beside `docs/` (`SECURITY.md`,
  `CHANGELOG.md`) included.
- The site (`mkdocs.yml`) is built by CI as an artifact for review. It is
  not published.

How others do it:

- Django's project template links
  `https://docs.djangoproject.com/en/{{ docs_version }}/...`, the
  documentation of its feature release [1, 2]. Its docs go in the sdist
  (`graft docs`), not in the wheel [3].
- SQLAlchemy's errors end with "Background on this error at:" and a link
  that names the version and the error's code [4]. pydantic's validation
  errors carry a link under `errors.pydantic.dev` that names the version
  [5].
- `pydoc` links a standard module to `https://docs.python.org/X.Y/library/`,
  unless `PYTHONDOCS` names a local copy [6].
- Tools that ship their prose ship it short, per error code:
  `rustc --explain E0382` [7], `ruff rule` [8].
- A package reads data files it ships with `importlib.resources` [9].

Options considered:

1. **Ship `docs/` in the wheel**, read with `importlib.resources`, and a
   command that prints a page or its path. Measured with poetry 2.2.1:
   the wheel grows from 2,644,669 to 3,949,764 bytes (+49%) and from 688
   to 921 files; installed, from 8.1 MB to 11.3 MB. Every install carries
   it, each production image included. Poetry puts a directory from
   outside `src/` into the package only through a second `packages`
   entry with `to = "domainkit"` [10]. poetry-core takes `docs/` as a
   package only because `docs/hooks/site.py` is a module; poetry's
   editable install, which every CI job and contributor uses, then adds
   the repository's root to `sys.path`; and an sdist built with the
   entry for the wheel alone fails to build the wheel again ("docs does
   not contain any element"). `include` would unpack `docs/` straight
   into `site-packages` [10]. The copy reads worse than GitHub's:
   admonitions and the API reference's mkdocstrings directives show as
   source, and links to files beside `docs/` lead nowhere.
2. **Links under the release's tag**, and a command that turns any name
   the code uses into its link. The wheel does not change. Reading needs
   a network. It is what the README, the project URLs, and `init`'s notes
   already do.
3. **Both**: option 1's size and packaging, to read offline.
4. **Write the links into the code at build time**, as the README's are:
   the wheel's code would differ from the code tested, and each docstring
   would carry a long URL.

## Decision

Option 2.

- `domainkit docs [reference ...]` prints the link of each page or
  decision named, one per line, for the release installed:
  - `docs/live.md`, or `live.md`, with its `#anchor` if any:
    `https://github.com/karimmattar/domainkit/blob/vX.Y.Z/docs/live.md`;
  - `ADR 0057`: that release's index of decisions,
    `docs/adr/README.md`, whose row links the record and says what
    amended or replaced it;
  - nothing: the first page, `docs/index.md`.

  Any other reference is refused, and nothing is printed. Like `init`,
  it needs no project; in a project, `python manage.py docs` does the
  same.
- The links name `v<version>` for every version a release may have, a
  pre-release included (`.github/scripts/release_check.py`), as the
  build job's links do. Any other version, such as a checkout's
  `0.0.0+unknown`, names `main`. `init`'s `docs_url` follows the same
  rule; it named `main` for a pre-release.
- The code keeps naming pages as `docs/<page>.md` and decisions as
  `ADR NNNN`. The package's docstring and the generated settings'
  docstrings say how to reach them. `init`'s last step for the fintech
  profile prints the link of `fintech.md`.
- `test_docs.py` checks that every page and decision named in `src/`,
  the templates included, in `benchmarks/`, the Makefile, and
  `pyproject.toml` links, through the command's `link`, to a file of the
  commit, at its anchor; and that every record cited exists. The build
  job's smoke test runs `domainkit docs docs/live.md` from the installed
  wheel and compares the link with the tag.
- An ADR number gives the index, not the record: a record's file name
  carries its title after the number, which a wheel without `docs/` does
  not know. The package would keep a second index of file names, in step
  with `docs/adr/README.md` on every record, to save one click, and the
  row also shows whether a later record changed the one cited.

## Consequences

- From a wheel, a page or a decision named in a docstring, a message, a
  hint, or a generated note leads to that release's text, rendered by
  GitHub, with its links. A name that leads nowhere fails a test.
- Reading needs github.com. Offline, a clone at the tag
  (`git clone --branch vX.Y.Z`) holds the same files.
- The wheel's size and files do not change.
- A wheel built from `main` between releases has the last release's
  version and links to its tag, whose pages may lack what `main` added.
  Only a release commit's build is published, as for the README.
- If the site is published one day, `link` can point there, and ADR
  numbers can link their records, without a change to what the code
  names.
- A domain command named `docs` replaces this one in its project, as for
  any built-in command.

## References

1. Django's project template, `settings.py-tpl`:
   <https://github.com/django/django/blob/main/django/conf/project_template/project_name/settings.py-tpl>
2. Django's `get_docs_version`, "Documentation is published per feature
   release":
   <https://github.com/django/django/blob/main/django/utils/version.py>
3. Django's `MANIFEST.in`:
   <https://github.com/django/django/blob/main/MANIFEST.in>
4. SQLAlchemy, `exc.py`, `HasDescriptionCode._code_str`:
   <https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/exc.py>
5. pydantic, validation errors and their `url`:
   <https://pydantic.dev/docs/validation/latest/errors/errors/>
6. Python, `pydoc`:
   <https://docs.python.org/3/library/pydoc.html>
7. rustc, `--explain`:
   <https://doc.rust-lang.org/rustc/command-line-arguments.html>
8. Ruff's command line, `ruff rule`:
   <https://docs.astral.sh/ruff/configuration/#full-command-line-interface>
9. Python, `importlib.resources`:
   <https://docs.python.org/3/library/importlib.resources.html>
10. Poetry, `packages` and `include`:
    <https://python-poetry.org/docs/pyproject/>
11. GitHub, permanent links to files:
    <https://docs.github.com/en/repositories/working-with-files/using-files/getting-permanent-links-to-files>
