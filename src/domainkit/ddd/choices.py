"""Enums whose members carry a label to show: Django's ``TextChoices``.

A field typed with one is stored by value, as any ``Enum``; the label is
for people (``get_FOO_display()`` in Django)::

    class Status(TextChoices):
        DRAFT = "draft"  # Label "Draft", from the name.
        PAID = "paid", gettext_lazy("Paid in full")

    Status.PAID.label  # "Paid in full", in the request's language
    Status.choices()  # [("draft", "Draft"), ("paid", "Paid in full")]
"""

from enum import IntEnum, StrEnum
from typing import Any, Self

__all__ = ["IntegerChoices", "TextChoices"]

type Label = object
"""A label: text, or what ``str()`` makes text of when it is read, as a
translation (``gettext_lazy``)."""


class _Labelled:
    """What ``TextChoices`` and ``IntegerChoices`` share: a label per member."""

    _label: Label | None
    name: str
    value: Any

    @property
    def label(self) -> str:
        """The member's label, or its name in words (``IN_FULL``: "In Full")."""
        if self._label is None:
            return self.name.replace("_", " ").title()
        return str(self._label)

    @classmethod
    def choices(cls) -> list[tuple[Any, str]]:
        """Each member's value and label, in order."""
        return [(member.value, member.label) for member in cls]  # type: ignore[attr-defined]

    @classmethod
    def labels(cls) -> dict[Any, str]:
        """Each member's label by its value."""
        return dict(cls.choices())


class TextChoices(_Labelled, StrEnum):
    """Text values, each with a label: ``PAID = "paid", "Paid in full"``."""

    def __new__(cls, value: str, label: Label | None = None) -> Self:
        """A member of ``value``, shown as ``label``."""
        member = str.__new__(cls, value)
        member._value_ = value
        member._label = label
        return member


class IntegerChoices(_Labelled, IntEnum):
    """Whole-number values, each with a label: ``HIGH = 3, "High"``."""

    def __new__(cls, value: int, label: Label | None = None) -> Self:
        """A member of ``value``, shown as ``label``."""
        member = int.__new__(cls, value)
        member._value_ = value
        member._label = label
        return member
