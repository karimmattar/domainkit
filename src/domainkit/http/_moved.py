"""The 2.8 paths of the modules that left ``domainkit.http`` in 2.9 (ADR 0195).

``domainkit.http.client`` and ``domainkit.http.guard`` hold no names of
their own: each is a view of its module in ``domainkit.outbound``. A name
is read there, and a name set or deleted on the old path (a 2.8 test's
``monkeypatch.setattr``, ``mock.patch``) is set or deleted there, where
the code reads it, so the adapters see the patch, and its undoing leaves
no copy behind.
"""

import sys
from types import ModuleType
from typing import Final

TARGET: Final = "__moved_to__"
"""The view's attribute that holds the module it shows."""


class MovedModule(ModuleType):
    """A module's 2.8 path: its names are another module's, read, set, and deleted."""

    def __setattr__(self, name: str, value: object) -> None:
        """Set ``name`` on the module moved to; the view's own names stay its own."""
        if _own(self, name):
            super().__setattr__(name, value)
        else:
            setattr(vars(self)[TARGET], name, value)

    def __delattr__(self, name: str) -> None:
        """Delete ``name`` from the module moved to; the view's own from the view."""
        if _own(self, name):
            super().__delattr__(name)
        else:
            delattr(vars(self)[TARGET], name)

    def __dir__(self) -> list[str]:
        """The view's own names, and every name of the module moved to."""
        return sorted({*vars(self), *dir(vars(self)[TARGET])})


def _own(view: ModuleType, name: str) -> bool:
    """Whether ``name`` is the view's: one it holds, or a module's dunder.

    ``__class__``, ``__spec__`` and the like are the view's own, set by
    the import system, or by a reload that makes the view again.
    """
    return name in vars(view) or (name.startswith("__") and name.endswith("__"))


def keep_path(name: str, target: ModuleType) -> None:
    """Make the module ``name`` a view of ``target``, which its names moved to.

    The module reads its other names from ``target`` with a ``__getattr__``
    of its own, typed for type checkers.
    """
    module = sys.modules[name]
    vars(module)[TARGET] = target
    module.__class__ = MovedModule
