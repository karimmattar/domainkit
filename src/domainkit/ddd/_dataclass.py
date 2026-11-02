"""Metaclasses that turn every subclass into the right kind of dataclass.

``dataclass_transform`` tells type checkers what the metaclass does at runtime,
so ``Product(id=..., name=...)`` is checked like an ordinary dataclass.
"""

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, cast, dataclass_transform

from domainkit.ddd._validation import (
    Checks,
    check_values,
    checking,
    checks_of,
    setter_for,
)


@dataclass_transform(frozen_default=True)
class ValueObjectMeta(type):
    """Make classes frozen dataclasses compared by value."""

    def __new__(
        mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]
    ) -> "ValueObjectMeta":
        """Create the class, then apply ``dataclass(frozen=True)``."""
        cls = super().__new__(mcs, name, bases, namespace)
        dataclass(frozen=True)(cast("type[Any]", cls))
        _check_after_init(cls)
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
        _clean_after_init(cls)
        if "__setattr__" in namespace:
            setattr(cls, "__setattr__", checking(namespace["__setattr__"]))  # noqa: B010 - a method.
        else:
            _check_on_set(cls)
        if "__repr__" not in namespace:
            generated = cast("Callable[[Any], str]", cls.__dict__["__repr__"])
            setattr(cls, "__repr__", _masked(generated))  # noqa: B010 - a method.
        return cls


def _check_after_init(cls: type) -> None:
    """Make a frozen class check its fields' markers once ``__init__`` is done.

    Its ``__init__`` is replaced on first use, when its hints resolve: by
    the dataclass's own if no field has markers.
    """
    init = cast("Callable[..., None]", cls.__dict__["__init__"])
    checks: Checks = {}

    def checked(self: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - any fields.
        init(self, *args, **kwargs)
        check_values(self, checks)

    @wraps(init)
    def first(self: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - any fields.
        nonlocal checks
        checks = checks_of(cls)
        chosen = wraps(init)(checked) if checks else init
        type.__setattr__(cls, "__init__", chosen)
        chosen(self, *args, **kwargs)

    setattr(cls, "__init__", first)  # noqa: B010 - a method.


def _check_on_set(cls: type) -> None:
    """Make an entity class check its fields' markers whenever one is set.

    Its ``__setattr__`` is replaced on first use, when its hints resolve: by
    ``object.__setattr__`` if no field has markers.
    """

    def first(self: Any, name: str, value: Any) -> None:  # noqa: ANN401 - any field.
        chosen = setter_for(cls)
        type.__setattr__(cls, "__setattr__", chosen)
        chosen(self, name, value)

    setattr(cls, "__setattr__", first)  # noqa: B010 - a method.


def _clean_after_init(cls: type) -> None:
    """Make an entity run ``clean()`` once ``__init__`` set its fields.

    Only for a class whose ``clean`` is its own: the base's does nothing.
    """
    clean = getattr(cls, "clean", None)
    if clean is None or getattr(clean, "__domainkit_noop__", False):
        return
    init = cast("Callable[..., None]", cls.__dict__["__init__"])

    @wraps(init)
    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - any fields.
        init(self, *args, **kwargs)
        self.clean()

    setattr(cls, "__init__", __init__)  # noqa: B010 - a method.


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
        _check_after_init(cls)
        return cls
