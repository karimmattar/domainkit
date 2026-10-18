"""Mark the event fields that hold personal data, and whose data it is."""

from dataclasses import dataclass
from functools import cache
from typing import Annotated, get_origin, get_type_hints


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

    Raises ``TypeError`` when a marker's ``of`` names no field of ``cls``.
    """
    hints = get_type_hints(cls, include_extras=True)
    fields: dict[str, Personal] = {}
    for name, hint in hints.items():
        if get_origin(hint) is not Annotated:
            continue
        for marker in hint.__metadata__:
            if not isinstance(marker, Personal):
                continue
            if marker.of not in hints or marker.of == name:
                raise TypeError(
                    f"{cls.__qualname__}.{name}: Personal(of={marker.of!r}) must "
                    "name another field of the class."
                )
            fields[name] = marker
    return fields
