"""Subscriptions: where events are sent, which ones, and the secret that signs them.

The secret never appears in a repr, an event, a log line, or a listing: it
is shown once, when it is made.
"""

import re
from collections.abc import Iterable
from dataclasses import field
from datetime import UTC, datetime, timedelta
from typing import Final, NewType, Self
from uuid import UUID, uuid4

from domainkit.ddd import AggregateRoot, BusinessRuleViolation, DomainEvent, NotFound

from .signing import new_secret

SubscriptionId = NewType("SubscriptionId", UUID)

ALL_EVENTS: Final = "*"
"""The event type that subscribes to every event."""
EVENT_TYPE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
MAX_EVENT_TYPES: Final = 50
MAX_DESCRIPTION: Final = 255


class SubscriptionNotFound(NotFound):
    """No webhook subscription has that id."""

    code = "webhook_subscription_not_found"


class InvalidEventTypes(BusinessRuleViolation):
    """A subscription's event types are missing or malformed."""

    code = "invalid_webhook_event_types"


class SubscriptionCreated(DomainEvent):
    """Staff subscribed a URL to events (the host only: URLs may hold tokens)."""

    subscription_id: SubscriptionId
    host: str
    event_types: tuple[str, ...]


class SubscriptionChanged(DomainEvent):
    """Staff changed a subscription; ``fields`` names what changed."""

    subscription_id: SubscriptionId
    fields: tuple[str, ...]


class SubscriptionSecretRotated(DomainEvent):
    """Staff replaced a subscription's secret."""

    subscription_id: SubscriptionId


class SubscriptionDeleted(DomainEvent):
    """Staff deleted a subscription."""

    subscription_id: SubscriptionId


def checked_event_types(event_types: Iterable[str]) -> list[str]:
    """Return the event types, without repeats, if all are valid."""
    checked = list(dict.fromkeys(event_types))
    if not 1 <= len(checked) <= MAX_EVENT_TYPES:
        raise InvalidEventTypes(
            f"A subscription needs 1 to {MAX_EVENT_TYPES} event types."
        )
    for event_type in checked:
        if event_type != ALL_EVENTS and not EVENT_TYPE.fullmatch(event_type):
            raise InvalidEventTypes(
                "Event types are '*' or up to 128 letters, digits, '_', '.', "
                "':', or '-'."
            )
    return checked


def _now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


class Subscription(AggregateRoot[SubscriptionId]):
    """A URL that receives the events it names, signed with its secret."""

    url: str
    event_types: list[str]
    secret: str = field(repr=False)
    active: bool = True
    description: str = ""
    previous_secret: str | None = field(default=None, repr=False)
    """The secret before the last rotation, still used until it expires."""
    previous_secret_expires_at: datetime | None = None
    created_at: datetime = field(default_factory=_now)

    @classmethod
    def create(
        cls,
        url: str,
        host: str,
        event_types: Iterable[str],
        description: str = "",
        *,
        active: bool = True,
    ) -> Self:
        """Subscribe ``url`` (checked by the caller) with a new secret."""
        subscription = cls(
            id=SubscriptionId(uuid4()),
            url=url,
            event_types=checked_event_types(event_types),
            secret=new_secret(),
            active=active,
            description=_description(description),
        )
        subscription.record(
            SubscriptionCreated(
                subscription_id=subscription.id,
                host=host,
                event_types=tuple(subscription.event_types),
            )
        )
        return subscription

    def wants(self, event_type: str) -> bool:
        """Whether this subscription receives ``event_type`` events."""
        return self.active and (
            ALL_EVENTS in self.event_types or event_type in self.event_types
        )

    def change(
        self,
        *,
        url: str | None = None,
        event_types: Iterable[str] | None = None,
        active: bool | None = None,
        description: str | None = None,
    ) -> None:
        """Change what is given; record which fields changed, if any."""
        changed = []
        if url is not None and url != self.url:
            self.url = url
            changed.append("url")
        if event_types is not None:
            checked = checked_event_types(event_types)
            if set(checked) != set(self.event_types):
                self.event_types = checked
                changed.append("event_types")
        if active is not None and active != self.active:
            self.active = active
            changed.append("active")
        if description is not None and description != self.description:
            self.description = _description(description)
            changed.append("description")
        if changed:
            self.record(
                SubscriptionChanged(subscription_id=self.id, fields=tuple(changed))
            )

    def rotate_secret(self, overlap: timedelta) -> str:
        """Make a new secret and return it; the old one signs for ``overlap`` more."""
        self.previous_secret = self.secret if overlap > timedelta(0) else None
        self.previous_secret_expires_at = (
            _now() + overlap if overlap > timedelta(0) else None
        )
        self.secret = new_secret()
        self.record(SubscriptionSecretRotated(subscription_id=self.id))
        return self.secret

    def signing_secrets(self, now: datetime) -> list[str]:
        """The secrets a delivery made at ``now`` is signed with."""
        return signing_secrets(
            self.secret, self.previous_secret, self.previous_secret_expires_at, now
        )

    def delete(self) -> None:
        """Record the deletion (the repository removes the row)."""
        self.record(SubscriptionDeleted(subscription_id=self.id))


def signing_secrets(
    secret: str,
    previous: str | None,
    previous_expires_at: datetime | None,
    now: datetime,
) -> list[str]:
    """The current secret, and the previous one until it expires."""
    if (
        previous is not None
        and previous_expires_at is not None
        and now < previous_expires_at
    ):
        return [secret, previous]
    return [secret]


def _description(text: str) -> str:
    """Check the length of a description."""
    if len(text) > MAX_DESCRIPTION:
        raise BusinessRuleViolation(
            f"A description has at most {MAX_DESCRIPTION} characters."
        )
    return text
