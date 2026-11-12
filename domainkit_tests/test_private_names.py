"""A module's underscore names are its own: no other module imports them.

A name that starts with an underscore says it may change without notice.
Once a second module imports it, the two change together or break, and
nothing says so. A helper two modules need gets a public name, in the
module that owns it or in one internal module they share (``auth._http``).

A package may keep names for its own modules only: ``from . import _b64``,
from the package's ``__init__``, stays inside the package. So may a module
whose own name starts with an underscore (``keys._http``): its package's
modules import from it, and no one else.

``ALLOWED`` may only shrink: a new import fails, and so does an entry that
is no longer needed.
"""

import ast
from pathlib import Path
from typing import Final

SOURCE: Final = Path(__file__).parents[1] / "src"

ALLOWED: Final[frozenset[tuple[str, str]]] = frozenset()
"""``(importer, private name)`` pairs still to be given public names."""


def module_of(path: Path, source: Path) -> str:
    """The dotted name of the module at ``path``, under ``source``."""
    parts = path.relative_to(source).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def imported_from(node: ast.ImportFrom, importer: str, is_package: bool) -> str:
    """The absolute name of the module ``node`` imports from."""
    if not node.level:
        return node.module or ""
    base = importer.split(".") if is_package else importer.split(".")[:-1]
    base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, *(node.module.split(".") if node.module else [])])


def is_package(name: str, source: Path) -> bool:
    """Whether ``name`` is a package under ``source``."""
    return (source.joinpath(*name.split(".")) / "__init__.py").exists()


def is_module(name: str, source: Path) -> bool:
    """Whether ``name`` is a module or a package under ``source``."""
    path = source.joinpath(*name.split("."))
    return path.with_suffix(".py").exists() or is_package(name, source)


def private_imports(source: Path = SOURCE) -> set[tuple[str, str]]:
    """``(importer, name)`` for each private name a module takes from another.

    Of the ``domainkit`` package under ``source``, its project templates
    left out.

    A private name starts with a single underscore, or is any name of a
    module that does. Not counted: what stays inside one package, as this
    module's docstring says.
    """
    found = set()
    for path in sorted((source / "domainkit").rglob("*.py")):
        if "templates" in path.relative_to(source).parts:
            continue
        importer = module_of(path, source)
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ImportFrom):
                continue
            origin = imported_from(node, importer, path.name == "__init__.py")
            if not origin.startswith("domainkit"):
                continue
            parts = origin.split(".")
            hidden = next((n for n, part in enumerate(parts) if part[:1] == "_"), None)
            for alias in node.names:
                if hidden is not None:  # A private module, or a name of one.
                    private, owner = True, ".".join(parts[:hidden])
                elif is_module(f"{origin}.{alias.name}", source):  # A module, whole.
                    private, owner = alias.name.startswith("_"), origin
                else:  # A package shares such names with its own modules.
                    private = alias.name.startswith("_") and alias.name[1:2] != "_"
                    owner = origin if is_package(origin, source) else ""
                inside = owner != "" and f"{importer}.".startswith(f"{owner}.")
                if private and not inside:
                    found.add((importer, f"{origin}.{alias.name}"))
    return found


def test_no_module_imports_anothers_private_name() -> None:
    """Every such import is listed in ``ALLOWED``, and nothing else is."""
    found = private_imports()

    assert found - ALLOWED == set(), "new imports of another module's private names"
    assert ALLOWED - found == set(), "no longer needed in ALLOWED: remove them"


def test_the_check_sees_private_names_and_private_modules(tmp_path: Path) -> None:
    """What it counts, and what stays inside a package."""
    package = tmp_path / "domainkit" / "sample"
    (package / "inner").mkdir(parents=True)
    (package / "__init__.py").write_text("_shared = 1\n")
    (package / "_helpers.py").write_text("def helper(): ...\n")
    (package / "one.py").write_text("_mine = 1\npublic = 2\n")
    (package / "two.py").write_text(
        "from . import _helpers, _shared\n"  # Its own package's: fine.
        "from ._helpers import helper\n"
        "from .one import _mine, public\n"  # A sibling's private name.
        "from domainkit.keys import _b64\n"  # Another package's.
        "from domainkit.keys._http import call_json\n"  # Its private module.
        "from domainkit.keys import _http\n"
        "from os import _exit\n"  # Not this tree's.
    )
    (package / "inner" / "__init__.py").write_text("")
    (package / "inner" / "deep.py").write_text(
        "from .. import _shared\n"  # A package's names reach its subpackages.
        "from .._helpers import helper\n"
        "from ..one import _mine\n"
    )

    found = private_imports(tmp_path)

    assert found == {
        ("domainkit.sample.two", "domainkit.sample.one._mine"),
        ("domainkit.sample.two", "domainkit.keys._b64"),
        ("domainkit.sample.two", "domainkit.keys._http.call_json"),
        ("domainkit.sample.two", "domainkit.keys._http"),
        ("domainkit.sample.inner.deep", "domainkit.sample.one._mine"),
    }


def test_relative_imports_resolve_from_modules_and_packages() -> None:
    """``.`` is a module's package, and a package itself."""
    assert imported_from(ast.ImportFrom("api", [], 1), "a.b.c", False) == "a.b.api"
    assert imported_from(ast.ImportFrom(None, [], 1), "a.b", True) == "a.b"
    assert imported_from(ast.ImportFrom("x", [], 2), "a.b.c", False) == "a.x"
    assert imported_from(ast.ImportFrom("a.x", [], 0), "a.b.c", False) == "a.x"
