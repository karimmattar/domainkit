"""Mark the event fields that hold personal data, and whose data it is.

``Counted`` marks fields the audit trail keeps only the size of.

Stores that keep copies of events or arguments (webhook deliveries, kept
responses) tag each copy with the subjects it is about (``subject_tag``),
so erasure finds it: the subjects between ``SEPARATOR``s, a control code
no subject holds.
"""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, is_dataclass
from functools import cache
from types import UnionType
from typing import Annotated, Final, Union, cast, get_args, get_origin, get_type_hints

from pydantic import BaseModel

SEPARATOR: Final = "\x1f"
"""Between the subjects of a tag, and around them: no subject holds it."""
SUBJECT: Final = re.compile(r"[^\x00-\x1f\x7f]{1,255}")
"""What can name a subject: 1 to 255 characters, without control codes."""


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


@dataclass(frozen=True, slots=True)
class Counted:
    """``Annotated`` metadata: the audit trail keeps how many items, not which.

    For collections that grow long or name people, such as the users a
    feature flag is on for::

        class FlagChanged(DomainEvent):
            users: Annotated[tuple[str, ...], Counted()]

    The event itself carries the items; only its audit row has the count.
    """


@cache
def counted_fields(cls: type) -> frozenset[str]:
    """The fields of ``cls`` marked ``Counted``."""
    hints = get_type_hints(cls, include_extras=True)
    return frozenset(
        name
        for name, hint in hints.items()
        if get_origin(hint) is Annotated
        and any(isinstance(marker, Counted) for marker in hint.__metadata__)
    )


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


def event_subjects(event: object) -> frozenset[str]:
    """Whom ``event``'s ``Personal`` fields are about: their ``of`` values, as text."""
    fields = personal_fields(cast(type, type(event)))
    return _subjects(fields, lambda name: getattr(event, name))


def subjects_in(
    holder: type | Callable[..., object], values: Mapping[str, object]
) -> frozenset[str]:
    """Whom ``values`` are about, by the ``Personal`` markers of ``holder``.

    ``holder`` is an event class (``values``, its JSON payload) or a task
    function (``values``, its arguments).
    """
    return _subjects(personal_fields(cast(type, holder)), values.get)


def _subjects(
    fields: Mapping[str, Personal], value: Callable[[str], object]
) -> frozenset[str]:
    """The text of each marker's ``of`` value that can name a subject."""
    found = (value(marker.of) for marker in fields.values())
    return frozenset(
        str(each) for each in found if each is not None and SUBJECT.fullmatch(str(each))
    )


def subject_tag(subjects: Iterable[str]) -> str | None:
    """``subjects`` as one text a store keeps; None for none.

    ``<US>ann<US>bob<US>`` (``US`` is ``SEPARATOR``): each subject between
    separators, sorted,
    so ``tagged_with`` finds one by its separators and never by a part of
    another.
    """
    found = sorted({subject for subject in subjects if SUBJECT.fullmatch(subject)})
    return SEPARATOR + SEPARATOR.join(found) + SEPARATOR if found else None


def tagged_with(subject: str) -> str:
    """What a tag holds when it names ``subject`` (see ``subject_tag``)."""
    return f"{SEPARATOR}{subject}{SEPARATOR}"
