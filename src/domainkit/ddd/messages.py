"""Commands and queries: the inputs of application use cases.

Both are frozen, keyword-only dataclasses that carry plain values. A command
asks the domain to change (``RenameProduct``); a query asks for data and
changes nothing (``ListProducts``).

A command that changes only some fields defaults the others to ``UNSET``,
so "leave it as it is" differs from "set it to None"::

    class UpdateProduct(Command):
        product_id: UUID
        name: str | Unset = UNSET
        description: str | None | Unset = UNSET

    if command.name is not UNSET:
        product.rename(ProductName(command.name))
"""

from typing import Final, Self

from domainkit.ddd._dataclass import MessageMeta


class Command(metaclass=MessageMeta):
    """Request to change state, named in the imperative."""


class Query(metaclass=MessageMeta):
    """Request for data that changes nothing."""


class Unset:
    """The type of ``UNSET``: a field the request left out, as opposed to None.

    There is one instance; it is false, equal only to itself, and copies
    and pickles of it are itself.
    """

    _instance: "Unset | None" = None

    def __new__(cls) -> Self:
        """Return the one instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance  # type: ignore[return-value]

    def __bool__(self) -> bool:
        """``UNSET`` is false, like None."""
        return False

    def __repr__(self) -> str:
        """``UNSET``."""
        return "UNSET"

    def __reduce__(self) -> str:
        """Pickle as the module's ``UNSET``, so unpickling gives the same object."""
        return "UNSET"

    def __copy__(self) -> Self:
        """The one instance."""
        return self

    def __deepcopy__(self, memo: object) -> Self:
        """The one instance."""
        return self


UNSET: Final = Unset()
"""A field left out of a partial change (``PatchModel``, ``UpdateX`` commands)."""
