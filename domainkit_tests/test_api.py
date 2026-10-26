"""``docs/api.md`` is the public API: it and every ``__all__`` agree.

The policy in ``docs/versioning.md`` covers exactly the names listed there.
A name listed must import from the module it is listed under; a name a
module puts in ``__all__`` must be listed; and a name the other pages tell
users to import must be among them.
"""

import ast
import re
from importlib import import_module
from pathlib import Path
from typing import Final

import pytest

ROOT: Final = Path(__file__).parents[1]
SRC: Final = ROOT / "src"
DOCS: Final = ROOT / "docs"
SECTION: Final = re.compile(r"^## `(domainkit(?:\.\w+)*)`$")
ITEM: Final = re.compile(r"^- `(\w+)`: \S")
DOTTED: Final = re.compile(r"`(domainkit(?:\.[A-Za-z_]\w*)+)(?:\([^`]*\))?`")
FROM_IMPORT: Final = re.compile(
    r"^\s*from (domainkit(?:\.\w+)*) import (\([^)]*\)|[^\n#]+)", re.MULTILINE
)


def listed(text: str) -> dict[str, list[str]]:
    """The names ``docs/api.md`` lists, by module, in order."""
    sections: dict[str, list[str]] = {}
    names: list[str] | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if match := SECTION.match(line):
            assert match[1] not in sections, f"line {number}: {match[1]} again"
            names = sections[match[1]] = []
        elif line.startswith("- `"):
            item = ITEM.match(line)
            assert item, f"line {number}: {line!r}"
            assert names is not None, f"line {number}: before any section"
            names.append(item[1])
    return sections


def exported(src: Path) -> dict[str, list[str]]:
    """Every module's ``__all__``, read from the source, by module."""
    modules: dict[str, list[str]] = {}
    for path in sorted((src / "domainkit").rglob("*.py")):
        if {"templates", "migration_env"} & set(path.parts):
            continue
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            ):
                parts = path.relative_to(src).with_suffix("").parts
                name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
                modules[name] = list(ast.literal_eval(node.value))
    return modules


def documented(text: str) -> set[str]:
    """Dotted names a page writes in backticks or imports in its examples."""
    names = set(DOTTED.findall(text))
    for module, imported in FROM_IMPORT.findall(text):
        for part in imported.strip("()").replace("\n", ",").split(","):
            name = part.split("#")[0].split(" as ")[0].strip()
            if name:
                names.add(f"{module}.{name}")
    return names


API: Final = listed((DOCS / "api.md").read_text(encoding="utf-8"))
EXPORTED: Final = exported(SRC)


def test_every_module_with_all_has_a_section_and_no_other_does() -> None:
    """A module's ``__all__`` makes it part of the API; its section says so."""
    assert sorted(set(EXPORTED) - set(API)) == [], "Add a section to docs/api.md."
    assert sorted(set(API) - set(EXPORTED)) == [], "Give the module an __all__."


@pytest.mark.parametrize("module", sorted(EXPORTED))
def test_each_section_lists_exactly_the_modules_all(module: str) -> None:
    """Each name once, all of them, in ``__all__``'s order."""
    assert API.get(module) == EXPORTED[module]


@pytest.mark.parametrize("module", sorted(API))
def test_every_listed_name_imports_from_its_module(module: str) -> None:
    """``from <module> import <name>`` works for every line."""
    imported = import_module(module)
    missing = [name for name in API[module] if not hasattr(imported, name)]
    assert missing == []


def test_the_pages_send_users_only_to_public_names() -> None:
    """A ``domainkit.x.Name`` a page shows is listed under ``domainkit.x``.

    Module paths (``domainkit.http.guard``) and names that are not
    attributes (logger names such as ``domainkit.security``) are not
    checked; nor is the changelog, which is history.
    """
    pages = [*sorted(DOCS.rglob("*.md")), ROOT / "README.md"]
    private = []
    for page in pages:
        for dotted in sorted(documented(page.read_text(encoding="utf-8"))):
            module, _, name = dotted.rpartition(".")
            if module in API and name in API[module]:
                continue
            if _is_module(dotted) or not _is_attribute(module, name):
                continue
            private.append(f"{page.name}: {dotted}")
    assert private == []


def test_the_checks_read_sections_items_and_examples() -> None:
    """The parsers find what they look for and refuse what they cannot read."""
    text = "## `domainkit.x`\n\nAbout.\n\n- `A`: One.\n- `b`: Two.\n"
    assert listed(text) == {"domainkit.x": ["A", "b"]}
    with pytest.raises(AssertionError, match="line 3"):
        listed("## `domainkit.x`\n\n- `A` without a summary\n")
    with pytest.raises(AssertionError, match="before any section"):
        listed("- `A`: One.\n")
    with pytest.raises(AssertionError, match="again"):
        listed("## `domainkit.x`\n## `domainkit.x`\n")
    page = (
        "Use `domainkit.http.middleware.CORSMiddleware(...)`.\n"
        "```python\nfrom domainkit.ddd import (\n    F,\n    Selection,  # here\n)\n"
        "from domainkit.clock import now as current\n```\n"
    )
    assert documented(page) == {
        "domainkit.http.middleware.CORSMiddleware",
        "domainkit.ddd.F",
        "domainkit.ddd.Selection",
        "domainkit.clock.now",
    }


def _is_module(dotted: str) -> bool:
    """Whether ``dotted`` names a module of the source tree."""
    path = SRC.joinpath(*dotted.split("."))
    return path.with_suffix(".py").exists() or (path / "__init__.py").exists()


def _is_attribute(module: str, name: str) -> bool:
    """Whether ``module`` imports and has ``name``."""
    if not _is_module(module):
        return False
    return hasattr(import_module(module), name)
