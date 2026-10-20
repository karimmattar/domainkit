"""What the code generators share: names, field types, and careful edits.

``startdomain`` makes a domain package; ``startaggregate``, ``addcommand``,
``addquery``, and ``addevent`` add to one. Generated names are checked
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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from domainkit.management.base import CommandError
from domainkit.utils.formatting import format_python

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
        """Whether a top-level statement defines or imports ``name``."""
        return name in top_level_names(self.tree())

    def add_imports(self, lines: Iterable[str]) -> None:
        """Add import statements after the last top-level import, once each."""
        tree = self.tree()
        imports = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
        new = [line for line in dict.fromkeys(lines) if line not in self.text]
        if not new:
            return
        at = imports[-1].end_lineno if imports else _after_docstring(tree)
        self._insert_lines(at or 0, new)

    def import_names(self, module: str, *names: str) -> None:
        """Import ``names`` from ``module``, those the file lacks."""
        known = top_level_names(self.tree())
        self.add_imports(
            f"from {module} import {name}" for name in names if name not in known
        )

    def append(self, block: str) -> None:
        """Add ``block`` at the end, two blank lines after what is there."""
        self.text = f"{self.text.rstrip()}\n\n\n{block.strip()}\n"

    def add_attribute(self, name: str, line: str) -> None:
        """Declare an attribute in class ``name``, after those it declares.

        Raises:
            CommandError: There is no such class.
        """
        node = self._find(ast.ClassDef, name)
        declared = [each for each in node.body if isinstance(each, ast.AnnAssign)]
        last = declared[-1] if declared else node.body[0]
        self._insert_lines(last.end_lineno or 0, [f"    {line}"])

    def add_to_class(self, name: str, lines: Sequence[str]) -> None:
        """Add ``lines`` at the end of the body of class ``name``.

        Raises:
            CommandError: There is no such class.
        """
        node = self._find(ast.ClassDef, name)
        self._insert_lines(
            node.end_lineno or 0, [f"    {line}" if line else "" for line in lines]
        )

    def add_to_function(self, name: str, lines: Sequence[str]) -> None:
        """Add ``lines`` at the end of function ``name`` (``Class.method``).

        Raises:
            CommandError: There is no such function.
        """
        node = self._find(ast.FunctionDef | ast.AsyncFunctionDef, name)
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
        self._insert_lines(found[-1], lines, blank=False)

    def save(self) -> bool:
        """Write the file if it changed; return whether it did."""
        if self.text == self.original:
            return False
        self.path.write_text(self.text, encoding="utf-8")
        return True

    def _find(
        self, kind: object, name: str
    ) -> ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef:
        """The class or function ``name`` (``Class.method`` inside a class).

        Raises:
            CommandError: There is none.
        """
        owner, _, member = name.rpartition(".")
        scope: ast.AST = self._find(ast.ClassDef, owner) if owner else self.tree()
        for node in ast.walk(scope):
            if isinstance(node, kind) and getattr(node, "name", None) == member:  # type: ignore[arg-type]
                return node  # type: ignore[return-value]
        raise CommandError(f"{self.path} has no {name}; was it renamed?")

    def _insert_lines(
        self, after: int, lines: Sequence[str], *, blank: bool = False
    ) -> None:
        """Insert ``lines`` after line ``after`` (1-based; 0 is the top)."""
        rows = self.text.splitlines()
        added = ["", *lines] if blank else list(lines)
        rows[after:after] = added
        self.text = "\n".join(rows) + "\n"


def top_level_names(tree: ast.Module) -> set[str]:
    """Names a module's top-level statements define or import."""
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
    if not (settings_path.is_file() and urls_path.is_file()):
        return []
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
        segment = ast.get_source_segment(settings.text, node.value) or ""
        if package in names or "#" in segment:
            return False
        rendered = (
            "[\n" + "".join(f'    "{name}",\n' for name in [*names, package]) + "]"
        )
        settings.text = settings.text.replace(segment, rendered, 1)
        return True
    return False


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
    valid value in Python (``example_imports`` for it) and ``json`` in a
    request. ``body`` is a request schema's annotation (default: ``plain``)
    and ``limits`` the arguments of its ``Field``. ``column`` says what
    ``table_for`` makes of it, for ``--help``.
    """

    annotation: str
    column: str
    plain: str = ""
    imports: tuple[tuple[str, str], ...] = ()
    example: str = '"text"'
    example_imports: tuple[tuple[str, str], ...] = ()
    json: str = '"text"'
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
        json="1",
        limits="ge=-(2**63), lt=2**63",
    ),
    "bool": FieldType("bool", "Boolean", example="True", json="true"),
    "float": FieldType("float", "Double", example="1.5", json="1.5"),
    "decimal": Bounded(
        "Annotated[Decimal, Digits({0}, {1})]",
        "ExactDecimal(precision, scale)",
        plain="Decimal",
        imports=(ANNOTATED, ("decimal", "Decimal"), ("domainkit.ddd", "Digits")),
        example='Decimal("1.5")',
        example_imports=(("decimal", "Decimal"),),
        json='"1.5"',
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
        json='"2026-01-02"',
    ),
    "datetime": FieldType(
        "datetime",
        "UTCDateTime",
        imports=(("datetime", "datetime"),),
        example="datetime(2026, 1, 2, tzinfo=UTC)",
        example_imports=(("datetime", "UTC"), ("datetime", "datetime")),
        json='"2026-01-02T03:04:05Z"',
        body="AwareDatetime",
    ),
    "uuid": FieldType(
        "UUID",
        "Uuid",
        imports=(("uuid", "UUID"),),
        example="UUID(int=1)",
        example_imports=(("uuid", "UUID"),),
        json='"00000000-0000-0000-0000-000000000001"',
    ),
    "ref": Referring(
        "Reference[{0}Id]",
        "Uuid, indexed",
        plain="UUID",
        imports=(("domainkit.ddd", "Reference"), ("uuid", "UUID")),
        example="UUID(int=1)",
        example_imports=(("uuid", "UUID"),),
        json='"00000000-0000-0000-0000-000000000001"',
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

    def default(self) -> str:
        """`` = None`` for a field that may be None, else nothing."""
        return " = None" if self.nullable else ""

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


@dataclass
class Domain:
    """A domain package ``startdomain`` made, and its tests, being added to."""

    name: str
    package: str
    directory: Path
    tests: Path
    root: Path
    sources: dict[Path, Source] = field(default_factory=dict)
    created: dict[Path, str] = field(default_factory=dict)

    @classmethod
    def locate(cls, name: str, directory: Path, tests: Path, root: Path) -> "Domain":
        """The domain ``name`` in ``directory``, inside the project ``root``.

        Raises:
            CommandError: It is outside the project, or not a domain package.
        """
        snake_name(name, "domain name")
        within(directory, root, "domain")
        if not (directory / "domain" / "model.py").is_file():
            raise CommandError(f"{directory} is not a domain made by startdomain.")
        package = ".".join(
            directory.resolve().relative_to(_source_root(directory)).parts
        )
        return cls(name, package, directory, tests, root)

    @property
    def camel(self) -> str:
        """The domain's name in CamelCase, as its units of work have it."""
        return "".join(part.capitalize() for part in self.name.split("_"))

    def module(self, layer: str) -> str:
        """The dotted name of ``<domain>.<layer>`` (``domain.model``)."""
        return f"{self.package}.{layer}"

    def edit(self, relative: str, *, tests: bool = False) -> Source:
        """The file to edit, read once.

        Raises:
            CommandError: It is missing or broken.
        """
        path = (self.tests if tests else self.directory) / relative
        if path not in self.sources:
            self.sources[path] = Source.read(path)
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


def _source_root(directory: Path) -> Path:
    """The directory on the import path: the first above without ``__init__.py``."""
    root = directory.resolve()
    while (root / "__init__.py").is_file():
        root = root.parent
    return root
