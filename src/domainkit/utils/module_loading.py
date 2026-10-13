"""Import objects by dotted path (``django.utils.module_loading``)."""

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
