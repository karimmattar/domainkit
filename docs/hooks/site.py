"""MkDocs hooks for the documentation site (``hooks:`` in ``mkdocs.yml``).

- **The API reference.** For every module ``docs/api.md`` lists, a page
  ``reference/api/<module>.md`` renders the docstrings of exactly the names
  listed there, with mkdocstrings, beside an index of the modules. The
  pages exist only in the build: ``docs/api.md`` is the public API (ADR
  0076) and the docstrings are the text, so neither is written twice, and a
  name added to both shows up without a change here.
- **Links out of ``docs/``.** A page may link a file beside the docs, such
  as ``../SECURITY.md``, which GitHub shows; on the site, that link goes to
  the file on GitHub instead, since the site holds only ``docs/``.
"""

import posixpath
import re
from pathlib import Path
from typing import Any

from mkdocs.structure.files import File, Files
from mkdocs.structure.pages import Page

REPOSITORY = "https://github.com/karimmattar/domainkit/blob/main/"
PREFIX = "reference/api"
_SECTION = re.compile(r"^## `(domainkit(?:\.\w+)*)`$")
_ITEM = re.compile(r"^- `(\w+)`: ")
_LINK = re.compile(r"\]\((?P<target>[^)\s#]+\.md)(?P<anchor>#[^)\s]*)?\)")


def modules(api: str) -> dict[str, tuple[str, list[str]]]:
    """Each module ``api.md`` lists: its summary line, and its public names."""
    found: dict[str, tuple[str, list[str]]] = {}
    current = ""
    for line in api.splitlines():
        if match := _SECTION.match(line):
            current = match[1]
            found[current] = ("", [])
        elif not current:
            continue
        elif item := _ITEM.match(line):
            found[current][1].append(item[1])
        elif line.strip() and not found[current][0] and not found[current][1]:
            found[current] = (line.strip(), [])
    return found


def module_page(module: str, names: list[str]) -> str:
    """The Markdown of one module's page: its docstring, then each name's."""
    members = "".join(f"\n        - {name}" for name in names)
    return (
        f"# `{module}`\n\n"
        f"::: {module}\n"
        "    options:\n"
        "      show_root_heading: false\n"
        f"      members:{members}\n"
    )


def index_page(found: dict[str, tuple[str, list[str]]]) -> str:
    """The Markdown of the reference's first page: each module and its summary."""
    lines = [
        "# API reference",
        "",
        "The docstrings of every public name, module by module: exactly the",
        "names [api.md](../../api.md) lists, which the compatibility policy",
        "covers ([versioning.md](../../versioning.md)).",
        "",
    ]
    lines += [
        f"- [`{module}`]({module}.md): {summary}"
        for module, (summary, _) in found.items()
    ]
    return "\n".join(lines) + "\n"


def on_files(files: Files, config: Any) -> Files:  # noqa: ANN401 - MkDocsConfig.
    """Add the API reference's pages to the site."""
    api = Path(config["docs_dir"], "api.md").read_text(encoding="utf-8")
    found = modules(api)
    files.append(
        File.generated(config, f"{PREFIX}/index.md", content=index_page(found))
    )
    for module, (_, names) in found.items():
        content = module_page(module, names)
        files.append(File.generated(config, f"{PREFIX}/{module}.md", content=content))
    return files


def on_page_markdown(
    markdown: str,
    page: Page,
    config: Any,  # noqa: ANN401 - MkDocsConfig.
    files: Files,
) -> str:
    """Point links to files beside ``docs/`` at the repository on GitHub."""
    root = Path(config["docs_dir"]).parent

    def outside(match: re.Match[str]) -> str:
        path = posixpath.normpath(
            posixpath.join(
                "docs", posixpath.dirname(page.file.src_uri), match["target"]
            )
        )
        if path.startswith("docs/") or not (root / path).is_file():
            return match[0]
        return f"]({REPOSITORY}{path}{match['anchor'] or ''})"

    return _LINK.sub(outside, markdown)
