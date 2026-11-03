"""What happens to a user's passkeys and devices (audited as ``devices.*``)."""

from uuid import UUID

from domainkit.ddd import DomainEvent

__all__ = ["DeviceBound", "DeviceUnbound", "PasskeyAdded", "PasskeyRemoved"]


class PasskeyAdded(DomainEvent):
    """A user registered a passkey."""

    user_id: UUID
    passkey_id: UUID


class PasskeyRemoved(DomainEvent):
    """A user removed a passkey."""

    user_id: UUID
    passkey_id: UUID


class DeviceBound(DomainEvent):
    """A user bound a device's key."""

    user_id: UUID
    device_id: UUID


class DeviceUnbound(DomainEvent):
    """A user unbound a device."""

    user_id: UUID
    device_id: UUID
