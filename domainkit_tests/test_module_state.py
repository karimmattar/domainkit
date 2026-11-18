"""Module state a test can leave behind has a public reset (ADR 0113, ADR 0195).

State here is what a module keeps for the process: a dict, list, set,
deque, ``OrderedDict``, ``defaultdict``, ``Counter``, ``WeakSet``, or weak
dictionary bound at module level that the module changes in place (an
item set or deleted, ``append``, ``add``, ``update``, ``pop``, ``clear``,
...), and a name a function rebinds through ``global``. A container never
changed is a table, not state. Objects that keep state inside themselves
(the handlers, hook points, ``PerLoop``) are not found by reading the
source; their resets are the plugin's (``domainkit.testing.RESETS``).

``RESETS`` names the public reset of each piece of state: a function, or a
context manager that puts it back as the block ends, listed in the
``__all__`` of the module it is named from, so a project's conftest needs
no private name. ``UNRESET`` lists what 2.9 keeps without one, each group
with its reason. It may only shrink: new state comes with its reset, and
an entry whose state is gone, or that gains a reset, leaves the list
(audit A28-ARCH-14).
"""

import ast
from collections.abc import Iterator
from importlib import import_module
from pathlib import Path
from typing import Final

from domainkit_tests.conftest import MakePackage

SRC: Final = Path(__file__).parents[1] / "src"

CONTAINERS: Final = frozenset(
    {
        "Counter",
        "OrderedDict",
        "WeakKeyDictionary",
        "WeakSet",
        "WeakValueDictionary",
        "bytearray",
        "defaultdict",
        "deque",
        "dict",
        "list",
        "set",
    }
)
"""What a module-level name is bound to when it may hold state: a call of these."""
MUTATORS: Final = frozenset(
    {
        "add",
        "append",
        "appendleft",
        "clear",
        "discard",
        "extend",
        "extendleft",
        "insert",
        "move_to_end",
        "pop",
        "popitem",
        "popleft",
        "remove",
        "setdefault",
        "update",
    }
)
"""Methods that change a container in place."""

RESETS: Final = {
    "domainkit.apps.hooks._points": "domainkit.apps.hooks.hooks_kept",
    "domainkit.contrib.flags.store._declared": "domainkit.contrib.flags.declared_kept",
    "domainkit.contrib.mail.admins._last_sent": (
        "domainkit.contrib.mail.admins.reset_reports"
    ),
    "domainkit.contrib.messaging.registry._tasks": (
        "domainkit.contrib.messaging.tasks_kept"
    ),
    "domainkit.contrib.notifications.push._apns_tokens": (
        "domainkit.contrib.notifications.push.reset_tokens"
    ),
    "domainkit.contrib.notifications.push._google_accounts": (
        "domainkit.contrib.notifications.push.reset_tokens"
    ),
    "domainkit.contrib.notifications.push._huawei_apps": (
        "domainkit.contrib.notifications.push.reset_tokens"
    ),
    "domainkit.http.metrics._refreshers": "domainkit.http.metrics.refreshers_kept",
    "domainkit.lifecycle._registered": "domainkit.lifecycle.unregister",
    "domainkit.log._throttled_logs": "domainkit.log.reset_throttled",
    "domainkit.money._registered": "domainkit.money.currencies_kept",
    # A stub's block puts back what it changed as it ends.
    "domainkit.outbound.client._everywhere": "domainkit.outbound.client.stub",
    "domainkit.outbound.client._shared_stub": "domainkit.outbound.client.stub",
    "domainkit.testing.queries._capturing": "domainkit.testing.CapturedQueries",
}
"""Each piece of state, and the public name that resets or restores it."""

UNRESET: Final = frozenset(
    {
        # Kept for a live object and gone with it: a transaction, a session,
        # an engine, an event loop, a function, an error, or the object
        # itself. Nothing outlives the test that made the object.
        "domainkit.contrib.audit.personal._held",
        "domainkit.contrib.keys.store._made",
        "domainkit.contrib.keys.store._used",
        "domainkit.contrib.messaging.apps._committing",
        "domainkit.contrib.messaging.dispatch._publishers",
        "domainkit.contrib.messaging.metrics._counted",
        "domainkit.contrib.messaging.metrics._counting",
        "domainkit.contrib.messaging.registry._marked",
        "domainkit.contrib.messaging.runs._inboxed",
        "domainkit.db.encrypted._FIELDS",
        "domainkit.db.encrypted._written",
        "domainkit.db.uow._ISOLATED",
        "domainkit.ddd.uow._MARKED",
        "domainkit.keys.ring._held",
        "domainkit.log._handlers",
        "domainkit.log._listening",
        "domainkit.outbound.client._clients",
        "domainkit.tasks._owned",
        # Worked out once from what does not change between tests (classes,
        # tables, settings objects, compiled catalogs), and worked out again
        # the same: a test cannot tell a kept answer from a new one.
        "domainkit.__version__",
        "domainkit.cache.redis._contexts",
        "domainkit.contrib.approvals.builtin._threshold",
        "domainkit.contrib.auth.principals._BULK_WATCHED",
        "domainkit.contrib.auth.principals._FACTORIES",
        "domainkit.contrib.auth.principals._INTERNED",
        "domainkit.contrib.idempotency.statements._built",
        "domainkit.contrib.mail.providers._clients",
        "domainkit.contrib.notifications.sms._clients",
        "domainkit.db.derive._column_types",
        "domainkit.db.encrypted._FOR_SUBJECTS",
        "domainkit.db.models._known",
        "domainkit.db.query._COMPILERS",
        "domainkit.db.repository._SHAPES",
        "domainkit.ddd.query._CHECKS",
        "domainkit.documents.base._made",
        "domainkit.i18n.formats._formats",
        "domainkit.i18n.lazy._found",
        "domainkit.i18n.translation._current",
        "domainkit.storage.scanning._made",
        # The process's own counts and locks, put back by what took them.
        "domainkit.contrib.audit.personal._forgets",
        "domainkit.db.models._mappings",
        "domainkit.debug.middleware._profiling",
        "domainkit.inspection._running",
        "domainkit.log._forking",
        "domainkit.log._handlers_lock",
        # Registrations made as modules load or the domains get ready, which
        # the next test needs too, or that no test undoes yet: publish a
        # reset for one, and move it to RESETS.
        "domainkit.apps.hooks._populating",
        "domainkit.contrib.auth.actions._spending_purposes",
        "domainkit.contrib.identity.providers._documents",
        "domainkit.contrib.identity.providers._key_sets",
        "domainkit.contrib.webhooks.delivery._exposed",
        "domainkit.ddd.events._actor_source",
        "domainkit.keys.ring._ring",
        "domainkit.ledger.trust._registered",
        "domainkit.management.generators.FIELD_TYPES",
        "domainkit.storage.cleanup._kept",
    }
)
"""Module state of 2.9 without a public reset, by why; it only shrinks."""


def module_state(root: Path, package: str) -> Iterator[str]:
    """``<module>.<name>`` for each piece of state the modules of ``package`` keep."""
    base = root.joinpath(*package.split("."))
    for path in sorted(base.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        if {"templates", "migration_env"} & set(parts):
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        module = ".".join(parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rebound = _rebound(tree)
        for name, value in _bindings(tree.body):
            if name in rebound or (_is_container(value) and _changed(tree, name)):
                yield f"{module}.{name}"


def _bindings(nodes: list[ast.stmt]) -> Iterator[tuple[str, ast.expr | None]]:
    """``(name, value)`` for each name a module binds, under ``if`` and ``try`` too."""
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    yield target.id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            yield node.target.id, node.value
        elif isinstance(node, ast.If):
            yield from _bindings(node.body)
            yield from _bindings(node.orelse)
        elif isinstance(node, ast.Try):
            for block in (node.body, node.orelse, node.finalbody):
                yield from _bindings(block)
            for handler in node.handlers:
                yield from _bindings(handler.body)


def _is_container(value: ast.expr | None) -> bool:
    """Whether ``value`` makes a container: a display, a comprehension, or a call."""
    shapes = (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)
    if isinstance(value, shapes):
        return True
    if not isinstance(value, ast.Call):
        return False
    called = value.func
    name = called.id if isinstance(called, ast.Name) else getattr(called, "attr", "")
    return name in CONTAINERS


def _changed(tree: ast.Module, name: str) -> bool:
    """Whether ``tree`` changes what ``name`` holds in place, at any depth."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in MUTATORS and _root(node.func.value) == name:
                return True
        elif isinstance(node, ast.Assign | ast.Delete):
            if any(_item_of(target, name) for target in node.targets):
                return True
        elif isinstance(node, ast.AugAssign) and (
            _item_of(node.target, name)
            or (isinstance(node.target, ast.Name) and node.target.id == name)
        ):
            return True
    return False


def _item_of(target: ast.expr, name: str) -> bool:
    """Whether ``target`` is an item of ``name`` (``name[key]``, ``name[a][b]``)."""
    return isinstance(target, ast.Subscript) and _root(target) == name


def _root(node: ast.expr) -> str | None:
    """The name under ``node``'s subscripts: ``table`` for ``table[a][b]``."""
    while isinstance(node, ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _rebound(tree: ast.Module) -> set[str]:
    """The names a function of ``tree`` declares ``global``."""
    return {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Global)
        for name in node.names
    }


def _public(path: str) -> object:
    """What the dotted ``path`` names, if its module's ``__all__`` lists it."""
    module, _, name = path.rpartition(".")
    imported = import_module(module)
    assert name in getattr(imported, "__all__", ()), f"{path} is not public"
    return getattr(imported, name)


def test_every_piece_of_module_state_is_reset_or_listed() -> None:
    """New state comes with its public reset; the list without one only shrinks."""
    found = set(module_state(SRC, "domainkit"))

    assert sorted(found - set(RESETS) - UNRESET) == [], "Give it a public reset."
    assert sorted((set(RESETS) | UNRESET) - found) == [], "Gone: remove it here."
    assert sorted(set(RESETS) & UNRESET) == []


def test_each_reset_is_a_public_callable() -> None:
    """A project's conftest calls it by a public name (ADR 0113)."""
    resets = {path: _public(path) for path in sorted(set(RESETS.values()))}

    assert [path for path, reset in resets.items() if not callable(reset)] == []


def test_the_check_finds_each_kind_of_state(
    make_package: MakePackage, tmp_path: Path
) -> None:
    """Containers changed in place, at any depth or in a branch, and globals."""
    make_package(
        "fakekit.store",
        {
            "__init__.py": "",
            "kept.py": """
                from collections import defaultdict
                from weakref import WeakSet

                TABLE = {"a": 1}
                NAMES = ["a"]
                _seen = set()
                _live = WeakSet()
                _nested: dict = {}
                _by_kind = defaultdict(list)
                _count = 0
                _last = None
                try:
                    _optional = {}
                except ImportError:
                    _fallback = []

                def remember(value):
                    global _last
                    _seen.add(value)
                    _live.add(value)
                    _nested["a"]["b"] = value
                    _by_kind[type(value)].append(value)
                    _optional[value] = value
                    _fallback.extend([value])
                    _last = value
                    local = {}
                    local["x"] = value

                def forget():
                    del _nested["a"]
            """,
        },
    )

    found = set(module_state(tmp_path, "fakekit"))

    assert found == {
        "fakekit.store.kept._seen",
        "fakekit.store.kept._live",
        "fakekit.store.kept._nested",
        "fakekit.store.kept._by_kind",
        "fakekit.store.kept._optional",
        "fakekit.store.kept._fallback",
        "fakekit.store.kept._last",
    }
