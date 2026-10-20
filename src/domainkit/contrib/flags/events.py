"""What happens to flags and dynamic settings; the audit trail records each."""

from domainkit.ddd import DomainEvent


class FlagChanged(DomainEvent):
    """A flag was created, or its rules changed."""

    name: str
    everyone: bool | None
    percentage: int
    users: tuple[str, ...]
    groups: tuple[str, ...]


class FlagDeleted(DomainEvent):
    """A flag was deleted; it is off for everyone again."""

    name: str


class SettingChanged(DomainEvent):
    """A dynamic setting got a new value (JSON)."""

    key: str
    value: object


class SettingReset(DomainEvent):
    """A dynamic setting went back to its default."""

    key: str
