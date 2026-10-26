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
