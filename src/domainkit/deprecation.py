"""How domainkit retires an API (the policy is in ``docs/versioning.md``).

A deprecated API keeps working for at least one minor release and warns with
``DomainkitDeprecationWarning`` each time it is used. Mark it with the
standard decorator, which type checkers also report::

    @warnings.deprecated(
        "Use Page.items; removed in domainkit 0.4.",
        category=DomainkitDeprecationWarning,
    )
    def results(self) -> list[T]: ...

A name that moved to another module keeps working from its old one: the old
module answers for it with ``moved``, which warns and hands out the new
name's object (the same object, so ``isinstance`` and ``except`` hold)::

    __getattr__ = moved(__name__, {"Require": "domainkit.http.access"}, "0.11")

A name that was renamed keeps its old name the same way, with ``renamed``,
which maps each old name to the new one's dotted path::

    __getattr__ = renamed(
        __name__, {"on_check": "domainkit.apps.hooks.on_ready"}, "2.0"
    )

Projects find their uses before upgrading by turning the warning into an
error in their test settings::

    filterwarnings = ["error::domainkit.deprecation.DomainkitDeprecationWarning"]
"""

import warnings
from collections.abc import Callable, Mapping
from importlib import import_module

__all__ = [
    "DomainkitDeprecationWarning",
    "moved",
    "renamed",
]


class DomainkitDeprecationWarning(DeprecationWarning):
    """A domainkit API that a later release removes; the message says what to use."""


def moved(
    module: str, names: Mapping[str, str], removed_in: str
) -> Callable[[str], object]:
    """A module ``__getattr__`` for ``names`` that moved: name to its new module.

    Each use warns with ``DomainkitDeprecationWarning``, naming the new
    place and the release that removes the old one, and returns the object
    the new module holds. Any other name raises ``AttributeError``, as a
    module without the hook would.
    """

    def __getattr__(name: str) -> object:
        target = names.get(name)
        if target is None:
            raise AttributeError(f"module {module!r} has no attribute {name!r}")
        warnings.warn(
            f"{module}.{name} moved to {target}.{name}; import it from there. "
            f"The old name is removed in {removed_in}.",
            DomainkitDeprecationWarning,
            stacklevel=2,
        )
        return getattr(import_module(target), name)

    return __getattr__


def renamed(
    module: str, names: Mapping[str, str], removed_in: str
) -> Callable[[str], object]:
    """A module ``__getattr__`` for ``names`` that were renamed: old name to new path.

    The new path is dotted, module and name (``domainkit.apps.hooks.on_ready``).
    Each use of an old name warns with ``DomainkitDeprecationWarning``, naming
    the new one and the release that removes the old, and returns the new
    name's object. Any other name raises ``AttributeError``.
    """

    def __getattr__(name: str) -> object:
        target = names.get(name)
        if target is None:
            raise AttributeError(f"module {module!r} has no attribute {name!r}")
        warnings.warn(
            f"{module}.{name} is now {target}; use the new name. "
            f"The old name is removed in {removed_in}.",
            DomainkitDeprecationWarning,
            stacklevel=2,
        )
        new_module, _, new_name = target.rpartition(".")
        return getattr(import_module(new_module), new_name)

    return __getattr__
