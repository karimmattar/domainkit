"""What happens to flags and dynamic settings; the audit trail records each."""

from typing import Annotated

from domainkit.ddd import Counted, DomainEvent


class FlagChanged(DomainEvent):
    """A flag was created, or its rules changed.

    The audit trail keeps how many users it names, not which (``Counted``).
    """

    name: str
    everyone: bool | None
    percentage: int
    users: Annotated[tuple[str, ...], Counted()]
    groups: tuple[str, ...]


class FlagDeleted(DomainEvent):
    """A flag was deleted; it is off for everyone again."""

    name: str


class SettingChanged(DomainEvent):
    """A dynamic setting got a new value (JSON).

    A secret setting's event says that it changed, with ``value`` None.
    """

    key: str
    value: object
    secret: bool = False


class SettingReset(DomainEvent):
    """A dynamic setting went back to its default."""

    key: str
