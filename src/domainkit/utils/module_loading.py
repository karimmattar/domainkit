"""Import objects by dotted path (``django.utils.module_loading``)."""

import sys
from collections.abc import Callable, Mapping
from importlib import import_module
from typing import Any


def import_string(dotted_path: str) -> Any:  # noqa: ANN401 - returns whatever the path names.
    """Return the attribute named by ``package.module.attr`` or ``package.module:attr``.

    Raises:
        ImportError: The path has no attribute part, or the attribute is missing.
    """
    module_path, _, attribute = dotted_path.replace(":", ".").rpartition(".")
    if not module_path or not attribute:
        raise ImportError(f"{dotted_path!r} is not a dotted path to an attribute.")
    module = import_module(module_path)
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise ImportError(
            f"Module {module_path!r} has no attribute {attribute!r}."
        ) from exc


def lazy_names(module: str, names: Mapping[str, str]) -> Callable[[str], object]:
    """A module ``__getattr__`` that imports ``names`` on first use: name to module.

    The module the name lives in is imported when the name is first read,
    and the name is then set on ``module``, so later reads are plain
    attribute reads. Any other name raises ``AttributeError``, as a module
    without the hook would. Type checkers see the names through imports
    under ``TYPE_CHECKING`` (PERF-15)::

        if TYPE_CHECKING:
            from domainkit.http.application import get_asgi_application

        __getattr__ = lazy_names(
            __name__, {"get_asgi_application": "domainkit.http.application"}
        )
    """

    def __getattr__(name: str) -> object:
        target = names.get(name)
        if target is None:
            raise AttributeError(f"module {module!r} has no attribute {name!r}")
        value = getattr(import_module(target), name)
        setattr(sys.modules[module], name, value)
        return value

    return __getattr__
