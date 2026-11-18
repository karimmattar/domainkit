"""The 2.8 paths of the modules that left ``domainkit.http`` in 2.9 (ADR 0195).

``domainkit.http.client`` and ``domainkit.http.guard`` hold no names of
their own: each is a view of its module in ``domainkit.outbound``. A name
is read there, and a name set on the old path (a 2.8 test's
``monkeypatch.setattr``, ``mock.patch``) is set there, where the code
reads it, so the adapters see the patch. The old path holds none of the
module's names as its own.
Deleting a name the module defined, while something replaced it, puts
back the value it had when the old path was first imported; deleting any
other name deletes it.
"""

import sys
from types import ModuleType
from typing import Final

TARGET: Final = "__moved_to__"
"""The view's attribute that holds the module it shows."""
DEFINED: Final = "__defined__"
"""The view's attribute that holds the names the module had, as the view was made."""


class MovedModule(ModuleType):
    """A module's 2.8 path: its names are another module's, read, set, and deleted."""

    def __setattr__(self, name: str, value: object) -> None:
        """Set ``name`` on the module moved to; the view's own names stay its own."""
        if _own(self, name):
            super().__setattr__(name, value)
        else:
            setattr(vars(self)[TARGET], name, value)

    def __delattr__(self, name: str) -> None:
        """Delete ``name`` from the module moved to; the view's own from the view.

        A name the module defined, replaced through the view, is put back
        rather than deleted: ``mock.patch`` deletes what it set on a name it
        took for not the view's own, and with ``create=True`` puts nothing
        back, which would leave the adapters without ``HttpClient``.
        """
        if _own(self, name):
            super().__delattr__(name)
            return
        target = vars(self)[TARGET]
        defined = vars(self)[DEFINED]
        if name in defined and vars(target).get(name) is not defined[name]:
            setattr(target, name, defined[name])
        else:
            delattr(target, name)

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
    vars(module)[DEFINED] = dict(vars(target))
    module.__class__ = MovedModule
