"""Metaclasses that turn every subclass into the right kind of dataclass.

``dataclass_transform`` tells type checkers what the metaclass does at runtime,
so ``Product(id=..., name=...)`` is checked like an ordinary dataclass.
"""

import dataclasses
from collections.abc import Callable
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
        if "__repr__" not in namespace:
            generated = cast("Callable[[Any], str]", cls.__dict__["__repr__"])
            setattr(cls, "__repr__", _masked(generated))  # noqa: B010 - a method.
        return cls


def _masked(generated: Callable[[Any], str]) -> Callable[[Any], str]:
    """The dataclass ``repr``, with each value of a field marked ``Encrypted`` masked.

    A value stored encrypted stays out of logs and tracebacks too, as a
    ``Sensitive`` value object's own ``repr`` keeps it out.
    """

    def __repr__(self: Any) -> str:  # noqa: ANN401 - any entity.
        from domainkit.ddd.fields import fields_of

        try:
            fields = fields_of(cast("Any", type(self)))
            hidden = {name for name, field in fields.items() if field.encrypted}
        except (NameError, TypeError):  # Hints that do not resolve yet.
            hidden = set()
        if not hidden:
            return generated(self)
        shown = ", ".join(
            f"{field.name}=<encrypted>"
            if field.name in hidden and getattr(self, field.name) is not None
            else f"{field.name}={getattr(self, field.name)!r}"
            for field in dataclasses.fields(self)
            if field.repr
        )
        return f"{type(self).__qualname__}({shown})"

    return __repr__


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
