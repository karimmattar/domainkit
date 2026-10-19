"""Squash a run of revisions into one (``manage.py squashmigrations``).

The squashed revision keeps the id of the newest revision it replaces and
the ``down_revision`` of the oldest, so databases at or past the newest one,
and revisions written after it, need no change. Its ``upgrade`` runs each
replaced revision's ``upgrade`` in order, and its ``downgrade`` each
``downgrade`` newest first: each body is copied as written, comments
included, into a function of its own.

What cannot stay correct is refused rather than guessed:

- a run with a merge, a branch, a branch label, or ``depends_on``;
- a revision with code outside ``upgrade`` and ``downgrade`` other than
  imports (helper functions, constants), or two that import different
  things under one name;
- a ``downgrade`` that raises: the run would not be reversible;
- a database of the target at a revision inside the run, other than the
  newest: it would be at a revision that no longer exists.

Databases this process cannot see (other environments) must also be at or
past the newest revision before the squashed file reaches them.
"""

import ast
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from alembic.script import Script, ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util.exc import CommandError as AlembicError

from domainkit.db import migrations
from domainkit.db.routing import router
from domainkit.utils.formatting import format_python

HEADER_NAMES: Final = frozenset(
    {"revision", "down_revision", "branch_labels", "depends_on"}
)
"""The module-level names every revision assigns."""
STANDARD_IMPORTS: Final = (
    "from collections.abc import Sequence",
    "import sqlalchemy as sa",
    "from alembic import op",
)
"""Imports every squashed file starts with, as the revision template's."""


class SquashRefused(Exception):
    """The revisions cannot be squashed without changing what they do."""


@dataclass(frozen=True, slots=True)
class _Revision:
    """One revision to squash: its script, source, and parsed pieces."""

    script: Script
    source: str
    imports: tuple[str, ...]
    upgrade: str
    downgrade: str

    @property
    def revision(self) -> str:
        """The revision's id."""
        return self.script.revision

    @property
    def function_suffix(self) -> str:
        """The id as part of a Python name."""
        return re.sub(r"\W", "_", self.revision)


def squash(
    target: str,
    *,
    start: str | None = None,
    end: str | None = None,
    message: str = "squashed",
) -> tuple[Path, list[str]]:
    """Squash ``start`` to ``end`` (default: first to head) of ``target``.

    Returns the new file and the revisions it replaces, oldest first.

    Raises:
        SquashRefused: The run cannot be squashed and stay correct.
    """
    migrations.check_message(message)
    config = migrations.alembic_config(target)
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    if len(heads) != 1:
        raise SquashRefused(
            f"The history of {target!r} has {len(heads)} heads; it needs one "
            "(run 'manage.py makemigrations --merge')."
        )
    run = _run(scripts, start, end or heads[0])
    revisions = [_parse(script) for script in run]
    _check_databases(target, [revision.revision for revision in revisions[:-1]])
    directory = Path(run[-1].path).parent
    path = directory / f"{run[-1].revision}_{_slug(message)}.py"
    source = _render(revisions, message)
    try:
        compile(source, str(path), "exec")
    except SyntaxError as exc:
        raise SquashRefused(f"The squashed revision would not compile: {exc}") from None
    originals = {Path(revision.script.path): revision.source for revision in revisions}
    for old in originals:
        old.unlink()
    path.write_text(source, encoding="utf-8")
    try:
        _check_replacement(target, run)
    except SquashRefused:
        path.unlink()
        for old, text in originals.items():
            old.write_text(text, encoding="utf-8")
        raise
    format_python([path])
    return path, [revision.revision for revision in revisions]


def _run(scripts: ScriptDirectory, start: str | None, end: str) -> list[Script]:
    """The revisions from ``start`` (default: the first) to ``end``, oldest first."""
    try:
        newest = scripts.get_revision(end)
        oldest = None if start is None else scripts.get_revision(start)
    except (RevisionError, AlembicError) as exc:
        raise SquashRefused(str(exc)) from None
    if newest is None or (start is not None and oldest is None):
        raise SquashRefused("Name revisions, not 'base'.")
    run: list[Script] = []
    current: Script | None = newest
    while current is not None:
        _check_linear(current, newest)
        run.append(current)
        if oldest is not None and current.revision == oldest.revision:
            break
        down = current.down_revision
        current = None if down is None else scripts.get_revision(str(down))
    else:
        if oldest is not None:
            raise SquashRefused(f"{oldest.revision} is not before {newest.revision}.")
    run.reverse()
    if len(run) < 2:
        raise SquashRefused("Squashing needs at least two revisions.")
    return run


def _check_linear(script: Script, newest: Script) -> None:
    """Refuse merges, branches, labels, and dependencies inside the run."""
    if isinstance(script.down_revision, tuple | list):
        raise SquashRefused(f"{script.revision} merges branches; squash after it.")
    if script.branch_labels or script.dependencies:
        raise SquashRefused(
            f"{script.revision} has branch labels or depends_on; it cannot be squashed."
        )
    if script is not newest and len(script.nextrev) > 1:
        raise SquashRefused(
            f"{script.revision} starts a branch; squash only a straight run."
        )


def _parse(script: Script) -> _Revision:
    """Read a revision's imports and bodies, refusing anything else."""
    path = Path(script.path)
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source, str(path))
    lines = source.splitlines()
    imports: list[str] = []
    bodies: dict[str, str] = {}
    for index, node in enumerate(module.body):
        if index == 0 and _is_docstring(node):
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            imports.append(ast.unparse(node))
        elif _assigns_header(node):
            continue
        elif (
            isinstance(node, ast.FunctionDef)
            and node.name in {"upgrade", "downgrade"}
            and not node.decorator_list
        ):
            bodies[node.name] = _body(node, lines)
        else:
            raise SquashRefused(
                f"{path.name} has code besides imports, upgrade, and downgrade "
                f"(line {getattr(node, 'lineno', '?')}); move it into them first."
            )
    if set(bodies) != {"upgrade", "downgrade"}:
        raise SquashRefused(f"{path.name} lacks an upgrade or a downgrade.")
    downgrade = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
    )
    if any(isinstance(node, ast.Raise) for node in downgrade.body):
        raise SquashRefused(
            f"{path.name} cannot be reverted (its downgrade raises), so the "
            "squashed revision could not be either."
        )
    return _Revision(
        script, source, tuple(imports), bodies["upgrade"], bodies["downgrade"]
    )


def _is_docstring(node: ast.stmt) -> bool:
    """Whether ``node`` is a string on its own (a docstring)."""
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _assigns_header(node: ast.stmt) -> bool:
    """Whether ``node`` assigns one of the revision's header names."""
    if isinstance(node, ast.AnnAssign):
        targets: list[ast.expr] = [node.target]
    elif isinstance(node, ast.Assign):
        targets = node.targets
    else:
        return False
    return all(
        isinstance(target, ast.Name) and target.id in HEADER_NAMES for target in targets
    )


def _body(function: ast.FunctionDef, lines: list[str]) -> str:
    """The function's statements as written, comments included, without its docstring.

    A body of only ``pass`` (and comments) is ``pass``.
    """
    statements = [each for each in function.body if not isinstance(each, ast.Pass)]
    first = function.body[0]
    if _is_docstring(first):
        statements = statements[1:]
    if not statements:
        return "    pass"
    start = first.end_lineno if _is_docstring(first) else statements[0].lineno - 1
    end = statements[-1].end_lineno
    return "\n".join(lines[start:end]).strip("\n")


def _check_databases(target: str, replaced: Sequence[str]) -> None:
    """Refuse when a database of ``target`` is at a revision that will go."""
    inside = set(replaced)
    for alias in router.aliases_of(router.target_of_alias(target)):
        current = migrations.status(alias)
        applied = [revision.revision for revision in current if revision.applied]
        at = applied[-1] if applied else None
        if at in inside:
            raise SquashRefused(
                f"Database {alias!r} is at {at}, which the squash replaces; "
                "migrate it to the newest revision of the run first."
            )


def _imports(revisions: Sequence[_Revision]) -> list[str]:
    """Every import the revisions use, once; a name bound twice is refused."""
    statements: list[str] = []
    bound: dict[str, str] = {}
    for statement in [*STANDARD_IMPORTS, *(i for r in revisions for i in r.imports)]:
        for name, target in _bindings(statement):
            if bound.setdefault(name, target) != target:
                raise SquashRefused(
                    f"Two revisions import different things as {name!r} "
                    f"({bound[name]} and {target})."
                )
        if statement not in statements:
            statements.append(statement)
    return statements


def _bindings(statement: str) -> list[tuple[str, str]]:
    """Each name an import statement binds, and the dotted path it binds."""
    node = cast(ast.Import | ast.ImportFrom, ast.parse(statement).body[0])
    found = []
    for alias in node.names:
        name = alias.asname or alias.name.partition(".")[0]
        if isinstance(node, ast.ImportFrom):
            target = f"{'.' * node.level}{node.module or ''}.{alias.name}"
        else:
            target = alias.name if alias.asname else name
        found.append((name, target))
    return found


def _render(revisions: Sequence[_Revision], message: str) -> str:
    """The squashed revision's source."""
    oldest, newest = revisions[0], revisions[-1]
    down = oldest.script.down_revision
    replaced = ", ".join(revision.revision for revision in revisions)
    title = message[:1].upper() + message[1:]
    lines = [
        f'"""{title}{"" if title.endswith(".") else "."}',
        "",
        f"Revision: {newest.revision}",
        f"Revises: {down or ''}",
        f"Created: {datetime.now(UTC):%Y-%m-%d %H:%M:%S}",
        f"Squashes: {replaced}",
        '"""',
        "",
        *_imports(revisions),
        "",
        f"revision: str = {newest.revision!r}",
        f"down_revision: str | None = {down!r}",
        "branch_labels: str | Sequence[str] | None = None",
        "depends_on: str | Sequence[str] | None = None",
        "",
        "",
        "def upgrade() -> None:",
        '    """Apply the squashed revisions, oldest first."""',
        *(f"    _upgrade_{r.function_suffix}()" for r in revisions),
        "",
        "",
        "def downgrade() -> None:",
        '    """Revert the squashed revisions, newest first."""',
        *(f"    _downgrade_{r.function_suffix}()" for r in reversed(revisions)),
    ]
    for revision in revisions:
        doc = (revision.script.doc or revision.revision).strip().replace('"""', "'''")
        for kind, body in (
            ("upgrade", revision.upgrade),
            ("downgrade", revision.downgrade),
        ):
            lines += [
                "",
                "",
                f"def _{kind}_{revision.function_suffix}() -> None:",
                f'    """{kind.capitalize()} {revision.revision}: {doc}"""',
                body,
            ]
    return "\n".join(lines) + "\n"


def _check_replacement(target: str, run: Sequence[Script]) -> None:
    """Re-read the history: the run's ends must be joined as before."""
    scripts = ScriptDirectory.from_config(migrations.alembic_config(target))
    try:
        squashed = scripts.get_revision(run[-1].revision)
    except (RevisionError, AlembicError) as exc:
        raise SquashRefused(f"The squashed history does not load: {exc}") from None
    if squashed is None or squashed.down_revision != run[0].down_revision:
        raise SquashRefused("The squashed revision does not join the history.")


def _slug(message: str) -> str:
    """A file-name part from ``message``, as Alembic makes them."""
    return re.sub(r"\W+", "_", message.lower()).strip("_")[:40] or "squashed"
