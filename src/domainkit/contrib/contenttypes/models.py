"""The content type entity, and the options a class declares in ``Meta``."""

from dataclasses import dataclass
from typing import Final

from domainkit.ddd import Entity
from domainkit.utils.text import snake_case

DEFAULT_PERMISSIONS: Final = ("add", "change", "delete", "view")
"""Actions that get a permission for every content type, unless ``Meta`` says."""


class ContentType(Entity[int]):
    """A kind of stored object: its domain label and lower-case class name."""

    domain: str
    model: str

    def __str__(self) -> str:
        """``<domain>.<model>``, as in ``billing.invoice``."""
        return f"{self.domain}.{self.model}"


@dataclass(frozen=True, slots=True)
class ModelOptions:
    """What a mapped class declares about itself (Django's ``Meta``)."""

    model: str
    """The class name in lower case: ``invoiceline``."""
    verbose_name: str
    """Human name, used in permission names: ``invoice line``."""
    default_permissions: tuple[str, ...]
    """Actions that get a ``<action>_<model>`` permission."""
    permissions: tuple[tuple[str, str], ...]
    """Extra ``(codename, name)`` permissions, such as ``approve_invoice``."""


def model_options(cls: type) -> ModelOptions:
    """Read ``cls.Meta``: ``verbose_name``, ``default_permissions``, ``permissions``.

    Every option is optional; ``Meta`` itself is too.
    """
    meta = getattr(cls, "Meta", None)
    verbose_name = getattr(meta, "verbose_name", None)
    return ModelOptions(
        model=cls.__name__.lower(),
        verbose_name=verbose_name or snake_case(cls.__name__).replace("_", " "),
        default_permissions=tuple(
            getattr(meta, "default_permissions", DEFAULT_PERMISSIONS)
        ),
        permissions=tuple(
            (str(codename), str(name))
            for codename, name in getattr(meta, "permissions", ())
        ),
    )
