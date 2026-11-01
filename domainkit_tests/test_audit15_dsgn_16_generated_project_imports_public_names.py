"""DSGN-16: the generated project imports only public names.

``docs/versioning.md`` makes the generated layout public, and ADR 0076 makes
public only what an ``__all__`` lists. Every project ``domainkit init``
writes imports ``domainkit.conf.env.Env`` and ``domainkit.http.health.router``,
from two modules that have no ``__all__``: by the policy, either may change in
any release, and every generated project would break with it.
``test_api.py`` skips the templates, so nothing notices.
"""

import re
from importlib import import_module
from pathlib import Path
from typing import Final

TEMPLATES: Final = Path(__file__).parents[1] / "src/domainkit/management/templates"
FROM_IMPORT: Final = re.compile(
    r"^\s*from (domainkit(?:\.\w+)*) import (\([^)]*\)|[^\n#]+)", re.MULTILINE
)


def imported_names() -> dict[str, set[str]]:
    """Each ``domainkit`` name a template imports, with the templates importing it."""
    found: dict[str, set[str]] = {}
    for path in sorted(TEMPLATES.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in FROM_IMPORT.finditer(text):
            body = re.sub(r"#[^\n]*", "", match[2]).strip("() \n")
            for part in body.split(","):
                name = part.strip().split(" as ")[0].strip()
                if name:
                    where = str(path.relative_to(TEMPLATES))
                    found.setdefault(f"{match[1]}:{name}", set()).add(where)
    return found


def is_public(module_name: str, name: str) -> bool:
    """Whether ``module_name`` lists ``name``, or ``name`` is a public module."""
    module = import_module(module_name)
    if name in getattr(module, "__all__", ()):
        return True
    try:
        submodule = import_module(f"{module_name}.{name}")
    except ImportError:
        return False
    return hasattr(submodule, "__all__")


def test_every_name_a_generated_project_imports_is_public() -> None:
    """``init`` and ``startdomain`` write imports of listed names only."""
    imports = imported_names()
    assert imports, "no template imports found"
    private = {
        key: sorted(where)
        for key, where in imports.items()
        if not is_public(*key.split(":"))
    }
    assert private == {}, (
        f"templates import names no __all__ lists (internal under ADR 0076): {private}"
    )
