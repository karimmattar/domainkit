"""Mark the event fields that hold personal data, and whose data it is."""

from dataclasses import dataclass, is_dataclass
from functools import cache
from types import UnionType
from typing import Annotated, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class Personal:
    """``Annotated`` metadata: the field holds personal data about someone.

    ``of`` names the field that identifies the person, usually their id::

        class UserCreated(DomainEvent):
            user_id: UUID
            username: Annotated[str, Personal(of="user_id")]

    The audit trail keeps such values only as pseudonyms, which forgetting
    the person makes unlinkable for good (``domainkit.contrib.audit``).
    """

    of: str
    """The field whose value identifies the person (the subject)."""


@cache
def personal_fields(cls: type) -> dict[str, Personal]:
    """The fields of ``cls`` marked ``Personal``, by name.

    A marker counts on the field itself, also on one member of a union
    (``Annotated[str, Personal(of="user_id")] | None``). Raises ``TypeError``
    when a marker's ``of`` names no other field of ``cls``, and when a field
    is not marked but a marker sits deeper, inside a list or the fields of
    another class: only whole fields are kept as pseudonyms, so the field
    itself must be marked.
    """
    hints = get_type_hints(cls, include_extras=True)
    fields: dict[str, Personal] = {}
    for name, hint in hints.items():
        markers, beneath = _field_markers(hint)
        if not markers and any(_holds_marker(inner, set()) for inner in beneath):
            raise TypeError(
                f"{cls.__qualname__}.{name}: Personal is read only on the field "
                "itself; mark the whole field, as Annotated[..., Personal(of=...)]."
            )
        for marker in markers:
            if marker.of not in hints or marker.of == name:
                raise TypeError(
                    f"{cls.__qualname__}.{name}: Personal(of={marker.of!r}) must "
                    "name another field of the class."
                )
            fields[name] = marker
    return fields


def _field_markers(hint: object) -> tuple[list[Personal], list[object]]:
    """The markers on a field itself, and the types beneath them."""
    union = get_origin(hint) in {Union, UnionType}
    markers: list[Personal] = []
    beneath: list[object] = []
    for member in get_args(hint) if union else (hint,):
        if get_origin(member) is Annotated:
            markers += [m for m in member.__metadata__ if isinstance(m, Personal)]
            beneath.append(member.__origin__)
        else:
            beneath.append(member)
    return markers, beneath


def _holds_marker(hint: object, seen: set[type]) -> bool:
    """Whether a ``Personal`` marker appears anywhere in ``hint``.

    Type arguments are searched, and the fields of dataclasses (value
    objects, events) and pydantic models, each class once.
    """
    if get_origin(hint) is Annotated:
        if any(isinstance(m, Personal) for m in hint.__metadata__):  # type: ignore[attr-defined]
            return True
        hint = hint.__origin__  # type: ignore[attr-defined]
    if arguments := get_args(hint):
        return any(_holds_marker(argument, seen) for argument in arguments)
    if not isinstance(hint, type) or hint in seen:
        return False
    if not (is_dataclass(hint) or issubclass(hint, BaseModel)):
        return False
    seen.add(hint)
    try:
        fields = get_type_hints(hint, include_extras=True)
    except NameError:  # Names only imported for type checkers.
        return False
    return any(_holds_marker(field, seen) for field in fields.values())
