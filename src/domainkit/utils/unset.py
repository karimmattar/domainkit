"""``UNSET``: a value left out, as opposed to None (``domainkit.ddd.UNSET``).

One sentinel for the whole framework: a field a partial change leaves out,
and a setting ``override_settings`` removes. It lives here, below every
layer, so ``domainkit.conf`` can use it without importing ``domainkit.ddd``.
"""

from typing import Final, Self


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
"""Left out: a field a partial change omits, a setting ``override_settings`` removes."""
