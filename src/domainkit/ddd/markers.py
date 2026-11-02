"""Field markers: ``Annotated`` limits a field's values keep everywhere.

``name: Annotated[str, MaxLength(100)]`` is checked by the object itself
whenever the field is set (``domainkit.ddd._validation``), by request
schemas before a use case runs, and by the database column it makes. Each
marker says what is wrong with a value in ``problem``; None means nothing.

Plain Python, imported by the dataclass machinery, so it imports nothing
of domainkit's.
"""

import re
from dataclasses import dataclass
from decimal import Decimal


def _is_number(value: object) -> bool:
    """An int, Decimal, or float, but not a bool."""
    return isinstance(value, (int, Decimal, float)) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class MaxLength:
    """``Annotated`` metadata: text of at most ``length`` characters."""

    length: int

    def __post_init__(self) -> None:
        """Refuse lengths below one."""
        if self.length < 1:
            raise ValueError(
                f"MaxLength needs a length of at least 1, not {self.length}."
            )

    def problem(self, value: object) -> str | None:
        """What is wrong with ``value``: text longer than ``length``."""
        if isinstance(value, str) and len(value) > self.length:
            return f"Use at most {self.length} characters."
        return None


@dataclass(frozen=True, slots=True)
class MinLength:
    """``Annotated`` metadata: text of at least ``length`` characters."""

    length: int

    def __post_init__(self) -> None:
        """Refuse lengths below one."""
        if self.length < 1:
            raise ValueError(
                f"MinLength needs a length of at least 1, not {self.length}."
            )

    def problem(self, value: object) -> str | None:
        """What is wrong with ``value``: text shorter than ``length``."""
        if isinstance(value, str) and len(value) < self.length:
            return f"Use at least {self.length} characters."
        return None


@dataclass(frozen=True, slots=True)
class Pattern:
    """``Annotated`` metadata: text that matches ``regex`` from end to end.

    The API checks it with pydantic's engine, which has no look-arounds or
    back-references; write ``[0-9]`` for digits, since a class of any digit
    also matches the digits of other scripts.
    """

    regex: str

    def __post_init__(self) -> None:
        """Refuse a pattern that does not compile."""
        try:
            re.compile(self.regex)
        except re.error as exc:
            raise ValueError(f"Pattern {self.regex!r} is not a regex: {exc}.") from None

    def problem(self, value: object) -> str | None:
        """What is wrong with ``value``: text that does not match, whole."""
        if isinstance(value, str) and re.fullmatch(self.regex, value) is None:
            return "Enter a value in the expected format."
        return None


@dataclass(frozen=True, slots=True)
class Range:
    """``Annotated`` metadata: a number from ``min`` to ``max``, both included.

    Either may be None, for no bound on that side.
    """

    min: int | Decimal | float | None = None
    max: int | Decimal | float | None = None

    def __post_init__(self) -> None:
        """Refuse an empty range, and one without a bound."""
        if self.min is None and self.max is None:
            raise ValueError("Range needs min, max, or both.")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"Range has min {self.min} above max {self.max}.")

    def problem(self, value: object) -> str | None:
        """What is wrong with ``value``: a number outside the range."""
        if not _is_number(value):
            return None
        if self.min is not None and value < self.min:  # type: ignore[operator]
            return f"Use {self.min} or more."
        if self.max is not None and value > self.max:  # type: ignore[operator]
            return f"Use {self.max} or less."
        return None


MARKERS: tuple[type, ...] = (MaxLength, MinLength, Pattern, Range)
"""The markers an object checks itself."""
