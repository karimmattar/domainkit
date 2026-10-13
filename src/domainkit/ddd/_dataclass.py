"""Metaclasses that turn every subclass into the right kind of dataclass.

``dataclass_transform`` tells type checkers what the metaclass does at runtime,
so ``Product(id=..., name=...)`` is checked like an ordinary dataclass.
"""

from dataclasses import dataclass
from typing import Any, cast, dataclass_transform


@dataclass_transform(frozen_default=True)
class ValueObjectMeta(type):
    """Make classes frozen dataclasses compared by value."""

    def __new__(
        mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]
    ) -> "ValueObjectMeta":
        """Create the class, then apply ``dataclass(frozen=True)``."""
        cls = super().__new__(mcs, name, bases, namespace)
        dataclass(frozen=True)(cast("type[Any]", cls))
        return cls


@dataclass_transform(eq_default=False, kw_only_default=True)
class EntityMeta(type):
    """Make classes keyword-only dataclasses that keep identity equality."""

    def __new__(
        mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]
    ) -> "EntityMeta":
        """Create the class, then apply ``dataclass(eq=False, kw_only=True)``."""
        cls = super().__new__(mcs, name, bases, namespace)
        dataclass(eq=False, kw_only=True)(cast("type[Any]", cls))
        return cls


@dataclass_transform(frozen_default=True, kw_only_default=True)
class MessageMeta(type):
    """Make classes frozen, keyword-only dataclasses (events and commands)."""

    def __new__(
        mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]
    ) -> "MessageMeta":
        """Create the class, then apply ``dataclass(frozen=True, kw_only=True)``."""
        cls = super().__new__(mcs, name, bases, namespace)
        dataclass(frozen=True, kw_only=True)(cast("type[Any]", cls))
        return cls
