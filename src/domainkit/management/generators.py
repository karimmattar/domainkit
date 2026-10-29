"""What the code generators share: names, field types, and careful edits.

``startdomain`` makes a domain package; ``startaggregate``, ``addcommand``,
``addquery``, and ``addevent`` add to one. Each is a ``Generator``: it plans
every change first, then writes them all (``plan``, then ``apply``); the
four that add to a domain are ``AggregateGenerator``, which takes the
domain and the aggregate and reports what it wrote. Generated names are checked
before anything is written: packages and fields are snake_case
identifiers, classes CamelCase, none a Python keyword, so no name can
reach outside the directory it is written in.

Edits to existing files are made only where the file still has the shape
the generators made: the class or function edited is found by name with
``ast``, and text is inserted at its end. Where a file was changed beyond
recognition, the edit is refused (``CommandError``), and nothing is
written: every file is edited in memory first.
"""

import ast
import keyword
import re
from abc import abstractmethod
from argparse import ArgumentParser
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Final

from domainkit.conf.env import project_root
from domainkit.management.base import BaseCommand, CommandError
from domainkit.utils.formatting import format_python
from domainkit.utils.text import plural_of, snake_case

__all__ = [
    "AggregateGenerator",
    "FieldType",
    "Generator",
    "register_field_type",
]

SNAKE: Final = re.compile(r"[a-z][a-z0-9_]*")
CAMEL: Final = re.compile(r"[A-Z][A-Za-z0-9]*")
SETTINGS_FILE: Final = Path("src/config/settings/base.py")
"""Where ``init`` puts ``INSTALLED_DOMAINS``."""
URLS_FILE: Final = Path("src/config/urls.py")
"""Where ``init`` puts the root router."""


def snake_name(name: str, what: str) -> str:
    """``name``, if it is a snake_case identifier and no keyword.

    Raises:
        CommandError: It is not.
    """
    if not SNAKE.fullmatch(name) or keyword.iskeyword(name):
        raise CommandError(f"{name!r} is not a valid snake_case {what}.")
    return name


def class_name(name: str, what: str = "class name") -> str:
    """``name``, if it is a CamelCase identifier.

    Raises:
        CommandError: It is not.
    """
    if not CAMEL.fullmatch(name):
        raise CommandError(f"{name!r} is not a valid CamelCase {what}.")
    return name


@dataclass
class Source:
    """A Python file being edited in memory; ``save`` writes it if changed."""

    path: Path
    text: str
    original: str = field(default="", repr=False)

    @classmethod
    def read(cls, path: Path) -> "Source":
        """Read ``path``.

        Raises:
            CommandError: It does not exist, or is not valid Python.
        """
        if not path.is_file():
            raise CommandError(
                f"{path} does not exist; was the domain made by startdomain?"
            )
        text = path.read_text(encoding="utf-8")
        source = cls(path, text, text)
        source.tree()
        return source

    def tree(self) -> ast.Module:
        """The parsed text.

        Raises:
            CommandError: It is not valid Python.
        """
        try:
            return ast.parse(self.text)
        except SyntaxError as exc:
            raise CommandError(f"{self.path} is not valid Python: {exc.msg}.") from None

    def defines(self, name: str) -> bool:
        """Whether a top-level statement defines or imports ``name``.

        ``Class.member`` asks whether class ``Class`` defines ``member``.

        Raises:
            CommandError: There is no class ``Class``.
        """
        owner, _, member = name.rpartition(".")
        if not owner:
            return name in top_level_names(self.tree())
        return member in top_level_names(self.find(ast.ClassDef, owner))  # type: ignore[arg-type]

    def annotation(self, owner: str, name: str) -> ast.expr | None:
        """The annotation of attribute ``name`` that class ``owner`` declares.

        Raises:
            CommandError: There is no class ``owner``.
        """
        node = self.find(ast.ClassDef, owner)
        for each in node.body:
            if (
                isinstance(each, ast.AnnAssign)
                and isinstance(each.target, ast.Name)
                and each.target.id == name
            ):
                return each.annotation
        return None

    def origin(self, name: str) -> str | None:
        """The module a top-level ``from ... import name`` takes ``name`` from."""
        for node in self.tree().body:
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and not node.level
                and any((alias.asname or alias.name) == name for alias in node.names)
            ):
                return node.module
        return None

    def classes(self) -> list[str]:
        """The names of the top-level classes, in order."""
        return [
            node.name for node in self.tree().body if isinstance(node, ast.ClassDef)
        ]

    def add_imports(self, lines: Iterable[str]) -> None:
        """Add import statements after the last top-level import, once each."""
        tree = self.tree()
        imports = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
        present = set(self.text.splitlines())
        new = [line for line in dict.fromkeys(lines) if line not in present]
        if not new:
            return
        at = imports[-1].end_lineno if imports else _after_docstring(tree)
        self._insert_lines(at or 0, new)

    def insert_after_imports(self, lines: Sequence[str]) -> None:
        """Insert ``lines`` after the last top-level import."""
        tree = self.tree()
        imports = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
        at = imports[-1].end_lineno if imports else _after_docstring(tree)
        self._insert_lines(at or 0, lines)

    def import_names(self, module: str, *names: str) -> None:
        """Import ``names`` from ``module``, those the file lacks."""
        known = top_level_names(self.tree())
        self.add_imports(
            f"from {module} import {name}" for name in names if name not in known
        )

    def insert_before_class(self, name: str, block: str) -> None:
        """Insert ``block`` before class ``name``, two blank lines apart.

        Raises:
            CommandError: There is no such class.
        """
        node = self.find(ast.ClassDef, name)
        first = min([node.lineno, *(each.lineno for each in node.decorator_list)])
        self._insert_lines(first - 1, [*block.strip().splitlines(), "", ""])

    def append(self, block: str) -> None:
        """Add ``block`` at the end, two blank lines after what is there."""
        self.text = f"{self.text.rstrip()}\n\n\n{block.strip()}\n"

    def add_attribute(self, name: str, line: str) -> None:
        """Declare an attribute in class ``name``, after those it declares.

        Raises:
            CommandError: There is no such class.
        """
        node = self.find(ast.ClassDef, name)
        declared = [each for each in node.body if isinstance(each, ast.AnnAssign)]
        last = declared[-1] if declared else node.body[0]
        self._insert_lines(last.end_lineno or 0, [f"    {line}"])

    def add_to_class(self, name: str, lines: Sequence[str]) -> None:
        """Add ``lines`` at the end of the body of class ``name``.

        Raises:
            CommandError: There is no such class.
        """
        node = self.find(ast.ClassDef, name)
        self._insert_lines(
            node.end_lineno or 0, [f"    {line}" if line else "" for line in lines]
        )

    def add_to_function(self, name: str, lines: Sequence[str]) -> None:
        """Add ``lines`` at the end of function ``name`` (``Class.method``).

        Raises:
            CommandError: There is no such function.
        """
        node = self.find(ast.FunctionDef | ast.AsyncFunctionDef, name)
        indent = " " * node.body[-1].col_offset
        self._insert_lines(node.end_lineno or 0, [f"{indent}{line}" for line in lines])

    def insert_before(self, pattern: str, lines: Sequence[str]) -> None:
        """Insert ``lines`` before the last top-level line that starts with ``pattern``.

        Raises:
            CommandError: No line does.
        """
        rows = self.text.splitlines()
        found = [n for n, row in enumerate(rows) if row.startswith(pattern)]
        if not found:
            raise CommandError(f"{self.path} has no line starting {pattern!r}.")
        self._insert_lines(found[-1], lines)

    def save(self) -> bool:
        """Write the file if it changed; return whether it did."""
        if self.text == self.original:
            return False
        self.path.write_text(self.text, encoding="utf-8")
        self.original = self.text
        return True

    def find(
        self, kind: object, name: str
    ) -> ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef:
        """The class or function ``name`` (``Class.method`` inside a class).

        Raises:
            CommandError: There is none.
        """
        owner, _, member = name.rpartition(".")
        scope: ast.AST = self.find(ast.ClassDef, owner) if owner else self.tree()
        for node in ast.walk(scope):
            if isinstance(node, kind) and getattr(node, "name", None) == member:  # type: ignore[arg-type]
                return node  # type: ignore[return-value]
        raise CommandError(f"{self.path} has no {name}; was it renamed?")

    def _insert_lines(self, after: int, lines: Sequence[str]) -> None:
        """Insert ``lines`` after line ``after`` (1-based; 0 is the top)."""
        rows = self.text.splitlines()
        rows[after:after] = lines
        self.text = "\n".join(rows) + "\n"


def top_level_names(tree: ast.Module | ast.ClassDef) -> set[str]:
    """Names a module's (or a class's) top-level statements define or import."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names |= {target.id for target in targets if isinstance(target, ast.Name)}
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names |= {
                alias.asname or alias.name.partition(".")[0] for alias in node.names
            }
    return names


def _after_docstring(tree: ast.Module) -> int:
    """The line after the module's docstring, or 0."""
    first = tree.body[0] if tree.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        return first.end_lineno or 0
    return 0


def install(package: str, domain: str, root: Path) -> list[str]:
    """Add ``package`` to ``INSTALLED_DOMAINS`` and mount its router, if safe.

    Safe means the project is laid out as ``init`` makes it, the settings
    list ``INSTALLED_DOMAINS`` literally (without comments inside), and
    neither file names the domain yet. Returns what was done; nothing if
    it was not safe, in which case the caller explains the steps instead.
    """
    settings_path, urls_path = root / SETTINGS_FILE, root / URLS_FILE
    try:
        settings, urls = Source.read(settings_path), Source.read(urls_path)
    except CommandError:
        return []
    if not (_add_installed(settings, package) and _mount(urls, package, domain)):
        return []
    settings.save()
    urls.save()
    format_python([settings_path, urls_path])
    return [
        f"Added {package!r} to INSTALLED_DOMAINS in {SETTINGS_FILE}.",
        f'Mounted its router at "/api/v1/{domain}" in {URLS_FILE}.',
    ]


def _add_installed(settings: Source, package: str) -> bool:
    """Add ``package`` to the literal ``INSTALLED_DOMAINS`` list; False if unsafe."""
    for node in settings.tree().body:
        if not (
            isinstance(node, ast.Assign)
            and [getattr(target, "id", None) for target in node.targets]
            == ["INSTALLED_DOMAINS"]
            and isinstance(node.value, ast.List)
        ):
            continue
        items = node.value.elts
        if not all(
            isinstance(item, ast.Constant) and isinstance(item.value, str)
            for item in items
        ):
            return False
        names = [item.value for item in items]  # type: ignore[attr-defined]
        start, end = _span(settings.text, node.value)
        if package in names or "#" in settings.text[start:end]:
            return False
        rendered = (
            "[\n" + "".join(f'    "{name}",\n' for name in [*names, package]) + "]"
        )
        settings.text = settings.text[:start] + rendered + settings.text[end:]
        return True
    return False


def _span(text: str, node: ast.expr) -> tuple[int, int]:
    """Where ``node`` starts and ends in ``text``, as character offsets.

    ``ast`` counts columns in UTF-8 bytes; each line is measured as such.
    """
    lines = text.splitlines(keepends=True)

    def offset(line: int, column: int) -> int:
        before = sum(len(each) for each in lines[: line - 1])
        return before + len(lines[line - 1].encode()[:column].decode())

    return (
        offset(node.lineno, node.col_offset),
        offset(node.end_lineno or node.lineno, node.end_col_offset or 0),
    )


def _mount(urls: Source, package: str, domain: str) -> bool:
    """Import the domain's router and include it in ``router``; False if unsafe."""
    module = f"{package}.api.routes"
    alias = f"{domain}_router"
    tree = urls.tree()
    if "router" not in top_level_names(tree) or urls.defines(alias):
        return False
    if any(
        isinstance(node, ast.ImportFrom) and node.module == module
        for node in ast.walk(tree)
    ):
        return False
    urls.add_imports([f"from {module} import router as {alias}"])
    urls.text += f'router.include_router({alias}, prefix="/api/v1/{domain}")\n'
    return True


def package_directory(package: str) -> Path:
    """The directory of the importable package ``package``.

    Raises:
        CommandError: It cannot be imported.
    """
    try:
        spec = find_spec(package)
    except ModuleNotFoundError:
        spec = None
    if spec is None or not spec.submodule_search_locations:
        raise CommandError(f"Parent package {package!r} cannot be imported.")
    return Path(next(iter(spec.submodule_search_locations)))


def within(path: Path, root: Path, what: str) -> Path:
    """``path``, if it lies inside ``root``.

    Raises:
        CommandError: It does not.
    """
    if not path.resolve().is_relative_to(root.resolve()):
        raise CommandError(f"The {what} {path} is outside the project {root}.")
    return path


@dataclass(frozen=True, slots=True)
class FieldType:
    """How the generators write fields of one kind (``--field name:kind:options``).

    ``annotation`` is the domain's hint and ``plain`` the type commands
    and events carry, each with ``{0}``, ``{1}`` for the options;
    ``imports`` are ``(module, name)`` pairs both may need. ``example`` is a
    valid value in Python (``example_imports`` for it), and ``request`` one
    in a request's JSON, written in Python. ``body`` is a request schema's
    annotation (default: ``plain``) and ``limits`` the arguments of its
    ``Field``. ``column`` says what ``table_for`` makes of it, for ``--help``.
    """

    annotation: str
    column: str
    plain: str = ""
    imports: tuple[tuple[str, str], ...] = ()
    example: str = '"text"'
    example_imports: tuple[tuple[str, str], ...] = ()
    request: str = '"text"'
    body: str = ""
    limits: str = ""
    defaults: tuple[str, ...] = ()
    """The options' defaults; their number is how many options it takes."""

    def check(self, options: Sequence[str]) -> None:
        """Refuse options this kind cannot take (they reach generated code)."""


@dataclass(frozen=True, slots=True)
class Bounded(FieldType):
    """A kind whose options are positive integers, up to ``limit``."""

    limit: int = 10_000_000

    def check(self, options: Sequence[str]) -> None:
        """Refuse options that are not whole numbers in range.

        Raises:
            CommandError: One is not.
        """
        for option in options:
            if not option.isdigit() or not 0 <= int(option) <= self.limit:
                raise CommandError(
                    f"{option!r} is not a number from 0 to {self.limit}."
                )
        if len(options) == 2 and int(options[1]) > int(options[0]):
            raise CommandError("An amount's scale cannot exceed its precision.")
        if options and int(options[0]) < 1:
            raise CommandError("A length or a precision is at least 1.")


@dataclass(frozen=True, slots=True)
class Referring(FieldType):
    """A reference to another aggregate, named by its class."""

    def check(self, options: Sequence[str]) -> None:
        """Refuse a class name that is not one.

        Raises:
            CommandError: It is not.
        """
        class_name(options[0], "aggregate name")


ANNOTATED: Final = ("typing", "Annotated")
FIELD: Final = ("pydantic", "Field")
FIELD_TYPES: Final[dict[str, FieldType]] = {
    "str": Bounded(
        "Annotated[str, MaxLength({0})]",
        "String(length)",
        plain="str",
        imports=(ANNOTATED, ("domainkit.ddd", "MaxLength")),
        limits="max_length={0}",
        defaults=("200",),
    ),
    "text": FieldType("str", "Text"),
    "int": FieldType(
        "int",
        "BigInteger",
        example="1",
        request="1",
        limits="ge=-(2**63), lt=2**63",
    ),
    "bool": FieldType("bool", "Boolean", example="True", request="True"),
    "float": FieldType("float", "Double", example="1.5", request="1.5"),
    "decimal": Bounded(
        "Annotated[Decimal, Digits({0}, {1})]",
        "ExactDecimal(precision, scale)",
        plain="Decimal",
        imports=(ANNOTATED, ("decimal", "Decimal"), ("domainkit.ddd", "Digits")),
        example='Decimal("{amount}")',
        example_imports=(("decimal", "Decimal"),),
        request='"{amount}"',
        limits="max_digits={0}, decimal_places={1}",
        defaults=("12", "2"),
        limit=38,
    ),
    "date": FieldType(
        "date",
        "Date",
        imports=(("datetime", "date"),),
        example="date(2026, 1, 2)",
        example_imports=(("datetime", "date"),),
        request='"2026-01-02"',
    ),
    "datetime": FieldType(
        "datetime",
        "UTCDateTime",
        imports=(("datetime", "datetime"),),
        example="datetime(2026, 1, 2, tzinfo=UTC)",
        example_imports=(("datetime", "UTC"), ("datetime", "datetime")),
        request='"2026-01-02T03:04:05Z"',
        body="AwareDatetime",
    ),
    "uuid": FieldType(
        "UUID",
        "Uuid",
        imports=(("uuid", "UUID"),),
        example="UUID(int=1)",
        example_imports=(("uuid", "UUID"),),
        request='"00000000-0000-0000-0000-000000000001"',
    ),
    "ref": Referring(
        "Reference[{0}Id]",
        "Uuid, indexed",
        plain="UUID",
        imports=(("domainkit.ddd", "Reference"), ("uuid", "UUID")),
        example="UUID(int=1)",
        example_imports=(("uuid", "UUID"),),
        request='"00000000-0000-0000-0000-000000000001"',
        defaults=("",),
    ),
}
"""Every ``--field`` kind; ``register_field_type`` adds more."""
RESERVED_FIELDS: Final = frozenset({"id", "version", "deleted_at"})


def register_field_type(name: str, kind: FieldType) -> None:
    """Let ``--field x:<name>`` generate fields of ``kind``."""
    FIELD_TYPES[snake_name(name, "field kind")] = kind


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One ``--field``: ``price:decimal:12:2``, ``due:date?`` (``?``: may be None)."""

    name: str
    kind: str
    options: tuple[str, ...]
    nullable: bool

    @classmethod
    def parse(cls, spec: str) -> "FieldSpec":
        """Read a ``--field`` value.

        Raises:
            CommandError: The name, kind, or options are not valid.
        """
        nullable = spec.endswith("?")
        name, _, rest = spec.removesuffix("?").partition(":")
        kind, *options = rest.split(":") if rest else ["str"]
        snake_name(name, "field name")
        if name in RESERVED_FIELDS:
            raise CommandError(f"{name!r} is set by domainkit; choose another name.")
        if kind not in FIELD_TYPES:
            raise CommandError(
                f"{kind!r} is not a field kind; use one of "
                f"{', '.join(sorted(FIELD_TYPES))}."
            )
        known = FIELD_TYPES[kind]
        if len(options) > len(known.defaults):
            raise CommandError(f"{spec!r} has more options than {kind} takes.")
        given = (*options, *known.defaults[len(options) :])
        if "" in given:
            raise CommandError(
                f"{spec!r} needs the aggregate it refers to: {name}:ref:Customer."
            )
        known.check(given)
        return cls(name, kind, tuple(given), nullable)

    @property
    def type(self) -> FieldType:
        """The kind's description."""
        return FIELD_TYPES[self.kind]

    def annotation(self) -> str:
        """The domain's type hint, with ``| None`` if it may be None."""
        return self._maybe(self.type.annotation.format(*self.options))

    def plain(self) -> str:
        """The type commands and events carry."""
        return self._maybe(self._bare())

    def _bare(self) -> str:
        """The plain type, without ``| None``."""
        return (self.type.plain or self.type.annotation).format(*self.options)

    def body(self) -> str:
        """A request schema's annotation, and its ``Field`` or default."""
        hint = self._maybe((self.type.body or self._bare()).format(*self.options))
        limits = self.type.limits.format(*self.options)
        if limits:
            default = "default=None, " if self.nullable else ""
            return f"{hint} = Field({default}{limits})"
        return f"{hint}{self.default()}"

    def example(self) -> str:
        """A valid value, in Python, as commands take it."""
        return self.type.example.format(amount=self._amount())

    def request(self) -> str:
        """A valid value of a request's JSON, written in Python."""
        return self.type.request.format(amount=self._amount())

    def _amount(self) -> str:
        """``1.5`` at the field's scale (``1.50``), as the database gives it back."""
        if self.kind != "decimal":
            return ""
        scale = int(self.options[1])
        return "1." + "5".ljust(scale, "0") if scale else "1"

    def default(self) -> str:
        """`` = None`` for a field that may be None, else nothing."""
        return " = None" if self.nullable else ""

    def domain_type(self) -> str:
        """The type a method takes: a reference is its aggregate's id."""
        if self.kind != "ref":
            return self.plain()
        return self._maybe(f"{self.options[0]}Id")

    def domain_example(self) -> str:
        """A valid value, as the domain holds it."""
        if self.kind == "ref":
            return f"{self.options[0]}Id({self.example()})"
        return self.example()

    def to_domain(self, value: str) -> str:
        """The expression that turns a command's ``value`` into the domain's."""
        if self.kind != "ref":
            return value
        converted = f"{self.options[0]}Id({value})"
        return (
            f"None if {value} is None else {converted}" if self.nullable else converted
        )

    def domain_imports(self) -> list[tuple[str, str]]:
        """What the domain module imports for the hint."""
        return list(self.type.imports)

    def plain_imports(self) -> list[tuple[str, str]]:
        """What commands and events import for the plain type."""
        return [
            item
            for item in self.type.imports
            if item[0] in {"decimal", "datetime", "uuid"}
        ]

    def _maybe(self, hint: str) -> str:
        """``hint | None`` for a field that may be None."""
        return f"{hint} | None" if self.nullable else hint


@dataclass(frozen=True, slots=True)
class Names:
    """The names generated for one aggregate: ``Category``, ``category``, ...

    ``plural`` names its table, its URLs, and its repository in the unit
    of work; ``title`` is how docstrings speak of it.
    """

    cls: str
    snake: str
    plural: str

    @classmethod
    def of(cls, aggregate: str, plural: str | None) -> "Names":
        """The names of the aggregate class ``aggregate``.

        Raises:
            CommandError: A name is not valid, or the plural is the singular.
        """
        snake = snake_case(class_name(aggregate, "aggregate name"))
        many = snake_name(plural or plural_of(snake), "plural")
        if plural is None and many == snake:
            raise CommandError(
                f"{aggregate} reads as a plural: name the aggregate in the singular."
            )
        if many == snake:
            raise CommandError(f"{many!r} is not a plural of {snake!r}.")
        return cls(aggregate, snake, many)

    @property
    def title(self) -> str:
        """``order line`` for ``OrderLine``."""
        return self.snake.replace("_", " ")


STARTED: Final = {
    "domain/events.py": '"""Facts the {domain} model records."""\n',
    "application/handlers.py": '"""Reactions to the {domain} events."""\n',
}
"""Modules of a domain the generators start, with these docstrings, if missing."""


@dataclass
class Domain:
    """A domain package ``startdomain`` made, and its tests, being added to."""

    name: str
    package: str
    directory: Path
    tests: Path
    sources: dict[Path, Source] = field(default_factory=dict)
    created: dict[Path, str] = field(default_factory=dict)

    @staticmethod
    def add_arguments(parser: ArgumentParser) -> None:
        """The arguments every generator takes: the domain and the aggregate."""
        parser.add_argument("domain", help="the domain's package name, e.g. catalog")
        parser.add_argument("aggregate", help="the aggregate's class, e.g. Product")
        parser.add_argument(
            "--plural", help="the aggregate's snake_case plural, for tables and URLs"
        )
        parser.add_argument(
            "--parent", default="domains", help="package that holds the domains"
        )
        parser.add_argument(
            "--tests-dir", type=Path, help="the domain's tests (default: tests/...)"
        )

    @classmethod
    def named(cls, name: str, parent: str, tests: Path | None) -> "Domain":
        """The domain ``name`` of the package ``parent``, in this project.

        Raises:
            CommandError: The names are not valid, or it is not a domain.
        """
        root = project_root()
        snake_name(name, "domain name")
        return cls.locate(
            name,
            package_directory(parent) / name,
            tests or root / "tests" / Path(*parent.split(".")) / name,
            root,
        )

    @classmethod
    def locate(cls, name: str, directory: Path, tests: Path, root: Path) -> "Domain":
        """The domain ``name`` in ``directory``, inside the project ``root``.

        Raises:
            CommandError: It is outside the project, or not a domain package.
        """
        within(directory, root, "domain")
        if not (directory / "domain" / "model.py").is_file():
            raise CommandError(f"{directory} is not a domain made by startdomain.")
        package = ".".join(
            directory.resolve().relative_to(_source_root(directory)).parts
        )
        return cls(name, package, directory, tests)

    @property
    def camel(self) -> str:
        """The domain's name in CamelCase, as its units of work have it."""
        return "".join(part.capitalize() for part in self.name.split("_"))

    def module(self, layer: str) -> str:
        """The dotted name of ``<domain>.<layer>`` (``domain.model``)."""
        return f"{self.package}.{layer}"

    def edit(self, relative: str, *, tests: bool = False) -> Source:
        """The file to edit, read once.

        The modules a smaller domain leaves out (``startdomain --minimal``:
        its events and their handlers) are started when missing.

        Raises:
            CommandError: It is missing or broken.
        """
        path = (self.tests if tests else self.directory) / relative
        if path not in self.sources:
            if not tests and relative in STARTED and not path.exists():
                return self.start(path, STARTED[relative].format(domain=self.name))
            self.sources[path] = Source.read(path)
        return self.sources[path]

    def start(self, path: Path, text: str) -> Source:
        """Plan a new Python file, to be edited as the others are.

        Raises:
            CommandError: It exists.
        """
        if path.exists() or path in self.created or path in self.sources:
            raise CommandError(f"{path} already exists.")
        self.sources[path] = Source(path, text)
        return self.sources[path]

    def create(self, path: Path, text: str) -> None:
        """Plan a new file.

        Raises:
            CommandError: It exists.
        """
        if path.exists() or path in self.created:
            raise CommandError(f"{path} already exists.")
        self.created[path] = text

    def apply(self) -> list[Path]:
        """Write every planned file and edit, then format them; return them."""
        written = []
        for path, text in self.created.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(path)
        written += [path for path, source in self.sources.items() if source.save()]
        format_python(written)
        return written


def parse_fields(specs: Iterable[str]) -> list[FieldSpec]:
    """Every ``--field``, each name once.

    Raises:
        CommandError: One is not valid, or a name repeats.
    """
    parsed = [FieldSpec.parse(spec) for spec in specs]
    if len({spec.name for spec in parsed}) != len(parsed):
        raise CommandError("Name each field once.")
    return parsed


def field_help() -> str:
    """``--field``'s help: the syntax, and every kind with its column."""
    kinds = ", ".join(
        f"{name} ({kind.column})" for name, kind in sorted(FIELD_TYPES.items())
    )
    return f"name:kind[:options][?] (? may be None), repeated; kinds: {kinds}"


def enumeration(items: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    return " and ".join([", ".join(items[:-1]), items[-1]] if items[1:] else items)


def words(name: str) -> str:
    """``products by owner`` for ``ProductsByOwner``."""
    return snake_case(name).replace("_", " ")


def _source_root(directory: Path) -> Path:
    """The directory on the import path: the first above without ``__init__.py``."""
    root = directory.resolve()
    while (root / "__init__.py").is_file():
        root = root.parent
    return root


class Generator[P](BaseCommand):
    """A scaffolding command: plan every change, then write them all.

    ``plan`` checks the names and reads what it edits, and raises
    ``CommandError`` before anything is written; ``apply`` writes the plan
    and returns what to print.
    """

    requires_setup = False

    @abstractmethod
    def plan(
        self,
        *args: Any,  # noqa: ANN401 - each command declares its own.
        **options: Any,  # noqa: ANN401 - each command's options.
    ) -> P:
        """Every change the command makes, checked, none written yet.

        Raises:
            CommandError: A name is invalid or taken, or a file is not as the
                generators made it.
        """

    @abstractmethod
    def apply(self, plan: P) -> str:
        """Write ``plan``; return the lines to print."""

    def handle(self, **options: Any) -> None:  # noqa: ANN401 - each command's options.
        """Plan, then apply, then print what was done."""
        self.write(self.apply(self.plan(**options)))


@dataclass(frozen=True, slots=True)
class Planned:
    """What an ``AggregateGenerator`` planned for one domain."""

    domain: Domain
    added: str
    """What the report names, as ``Product`` or ``PublishProduct``."""
    next_steps: str = ""


class AggregateGenerator(Generator[Planned]):
    """A generator that adds to an aggregate of a domain ``startdomain`` made.

    It takes the domain, the aggregate, ``--plural``, ``--parent``, and
    ``--tests-dir`` (``Domain.add_arguments``), and options of its own
    (``add_options``); ``plan_for`` plans the edits on the ``Domain``.
    """

    def add_arguments(self, parser: ArgumentParser) -> None:
        """The domain and the aggregate, then the command's own options."""
        Domain.add_arguments(parser)
        self.add_options(parser)

    def add_options(self, parser: ArgumentParser) -> None:
        """Add the options this generator takes besides the domain's."""

    @abstractmethod
    def plan_for(
        self,
        *args: Any,  # noqa: ANN401 - each command declares (domain, names, ...).
        **options: Any,  # noqa: ANN401 - each command's own options.
    ) -> Planned:
        """Plan the edits on a ``Domain`` for the aggregate's ``Names``.

        It is called with the domain, the names, and the command's own
        options as keywords.

        Raises:
            CommandError: A name is invalid or taken, or a file is not as the
                generators made it.
        """

    def plan(
        self,
        *,
        domain: str,
        aggregate: str,
        plural: str | None,
        parent: str,
        tests_dir: Path | None,
        **options: Any,  # noqa: ANN401 - the command's own options.
    ) -> Planned:
        """Find the domain, name the aggregate, and plan the edits."""
        target = Domain.named(domain, parent, tests_dir)
        return self.plan_for(target, Names.of(aggregate, plural), **options)

    def apply(self, plan: Planned) -> str:
        """Write every planned edit; name the files written, and what is next."""
        written = plan.domain.apply()
        lines = [f"Added {plan.added} to {plan.domain.package}:"]
        lines += [f"  {path}" for path in written]
        if plan.next_steps:
            lines.append(plan.next_steps)
        return "\n".join(lines)
