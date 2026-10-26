"""Apply a tutorial's code edits to a generated project, as a reader would.

A tutorial page shows each edit as a Python block titled with the file it
changes (```` ```python title="src/.../model.py" ````), and says in words
what to do with it. This module does the same to the file, so the page's
code runs as written. Each top-level statement of the block is one of
these:

- an ``import`` or ``from ... import``: added after the file's last
  top-level import, unless the file has that line already;
- a ``class`` whose body starts with ``...``: the rest of its body goes
  into the file's class of that name: fields after its last field, a
  method in place of the one of its name, or else at the class's end;
- a ``def`` or ``class`` whose name the file defines once, at any depth:
  that definition is replaced, at its indentation;
- a ``def`` or ``class`` the file does not define, or any other statement:
  added at the end of the file.

What cannot be placed (no such file, a name defined twice, an addition to
a class the file lacks) is refused, so a page cannot show an edit this
harness would apply differently from a reader.
"""

import ast
import textwrap
from pathlib import Path


class Refused(Exception):
    """An edit the harness does not understand, or cannot place."""


def apply_edit(path: Path, code: str) -> None:
    """Change ``path`` as the block ``code`` says.

    A ``*`` in the file's name stands for what a command chose, such as a
    migration's revision; exactly one file must match.

    Raises:
        Refused: No file or several match, or a definition is ambiguous, or
            the block adds to a class the file lacks.
    """
    if "*" in path.name:
        matches = sorted(path.parent.glob(path.name))
        if len(matches) != 1:
            raise Refused(f"{len(matches)} files match {path}")
        [path] = matches
    if not path.is_file():
        raise Refused(f"{path} does not exist")
    block = ast.parse(code)
    lines = code.splitlines()
    for node in block.body:
        source = "\n".join(lines[_start(node) - 1 : node.end_lineno])
        text = path.read_text(encoding="utf-8")
        if isinstance(node, ast.Import | ast.ImportFrom):
            text = _add_import(text, source)
        elif isinstance(node, ast.ClassDef) and _is_addition(node):
            text = _add_to_class(text, node, lines)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            text = _replace_or_add(text, node.name, source)
        else:
            text = _append(text, source)
        path.write_text(text, encoding="utf-8")


def _start(node: ast.stmt) -> int:
    """The first line of ``node``, its decorators included."""
    decorators: list[ast.expr] = getattr(node, "decorator_list", [])
    return min([node.lineno, *(each.lineno for each in decorators)])


def _is_addition(node: ast.ClassDef) -> bool:
    """Whether the class body starts with ``...``: an addition, not a whole class."""
    first = node.body[0]
    return (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and first.value.value is Ellipsis
    )


def _add_import(text: str, line: str) -> str:
    """``text`` with the import ``line`` after its last top-level import."""
    lines = text.splitlines()
    if line in lines:
        return text
    imports = [
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    end = imports[-1].end_lineno if imports else 0
    assert end is not None
    return "\n".join([*lines[:end], line, *lines[end:]]) + "\n"


def _definitions(text: str, name: str) -> list[ast.stmt]:
    """Every ``def`` or ``class`` called ``name`` in ``text``, at any depth."""
    kinds = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    return [
        node
        for node in ast.walk(ast.parse(text))
        if isinstance(node, kinds) and node.name == name
    ]


def _replace_or_add(text: str, name: str, source: str) -> str:
    """``text`` with the definition of ``name`` replaced by ``source``, or added."""
    found = _definitions(text, name)
    if not found:
        return _append(text, source)
    if len(found) > 1:
        raise Refused(f"{name} is defined {len(found)} times; which one?")
    [node] = found
    lines = text.splitlines()
    indented = textwrap.indent(source, " " * node.col_offset)
    end = node.end_lineno
    assert end is not None
    return "\n".join([*lines[: _start(node) - 1], indented, *lines[end:]]) + "\n"


def _add_to_class(text: str, addition: ast.ClassDef, lines: list[str]) -> str:
    """``text`` with the fields and methods of ``addition`` added to its class."""
    found = _definitions(text, addition.name)
    target = found[0] if len(found) == 1 else None
    if not isinstance(target, ast.ClassDef):
        raise Refused(f"no single class {addition.name} to add to")
    fields = [node for node in target.body if isinstance(node, ast.AnnAssign)]
    last_field = (fields[-1] if fields else target.body[0]).end_lineno
    assert last_field is not None
    assert target.end_lineno is not None
    methods = {
        node.name: node
        for node in target.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    indent = " " * target.body[0].col_offset
    # (first line, line after the last) to replace; equal for an insertion.
    splices: list[tuple[int, int, list[str]]] = []
    for node in addition.body[1:]:
        source = textwrap.dedent("\n".join(lines[_start(node) - 1 : node.end_lineno]))
        new = textwrap.indent(source, indent)
        name = getattr(node, "name", None)
        if isinstance(node, ast.AnnAssign):
            splices.append((last_field, last_field, [new]))
        elif name in methods:
            old = methods[name]
            assert old.end_lineno is not None
            splices.append((_start(old) - 1, old.end_lineno, [new]))
        else:
            splices.append((target.end_lineno, target.end_lineno, ["", new]))
    file_lines = text.splitlines()
    # From the bottom up, so each position still holds, and the last of
    # those at one place first, so they keep the block's order.
    for start, end, new_lines in sorted(splices, key=lambda each: each[0])[::-1]:
        file_lines[start:end] = new_lines
    return "\n".join(file_lines) + "\n"


def _append(text: str, source: str) -> str:
    """``text`` with ``source`` at its end, after two blank lines."""
    return text.rstrip("\n") + "\n\n\n" + source + "\n"
