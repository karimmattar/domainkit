"""Subscriptions: where events are sent, which ones, and the secret that signs them.

The secret never appears in a repr, an event, a log line, or a listing: it
is shown once, when it is made. The table holds it encrypted
(``domainkit.encryption``, under a key derived from ``SECRET_KEY``) and
bound to its subscription's id, so a value copied to another row does not
decrypt. Deliveries decrypt it to sign; a rotation, to carry it over.
"""

import re
from collections.abc import Iterable
from dataclasses import field
from datetime import datetime, timedelta
from typing import Final, NewType, Self
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from domainkit import clock
from domainkit.ddd import AggregateRoot, BusinessRuleViolation, DomainEvent, NotFound
from domainkit.encryption import DecryptionFailed, SecretBox

from .signing import new_secret

SubscriptionId = NewType("SubscriptionId", UUID)

ALL_EVENTS: Final = "*"
"""The event type that subscribes to every event."""
EVENT_TYPE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
MAX_EVENT_TYPES: Final = 50
MAX_DESCRIPTION: Final = 255
MAX_OWNER: Final = 255
SECRET_PURPOSE: Final = "domainkit.webhooks.secret"  # noqa: S105 - a purpose.
"""What the key that encrypts subscription secrets is derived for."""


class SubscriptionNotFound(NotFound):
    """No webhook subscription has that id."""

    code = "webhook_subscription_not_found"


class InvalidEventTypes(BusinessRuleViolation):
    """A subscription's event types are missing or malformed."""

    code = "invalid_webhook_event_types"


class SubscriptionCreated(DomainEvent):
    """A URL was subscribed to events (the host only: URLs may hold tokens).

    ``owner`` is the subject that subscribed it through the self-service
    API; None when staff did.
    """

    subscription_id: SubscriptionId
    host: str
    event_types: tuple[str, ...]
    owner: str | None = None


class SubscriptionChanged(DomainEvent):
    """Staff changed a subscription; ``fields`` names what changed.

    When the URL changed, ``host`` is its new host and ``previous_host`` the
    one before (hosts only: paths and queries may hold tokens).
    """

    subscription_id: SubscriptionId
    fields: tuple[str, ...]
    host: str | None = None
    previous_host: str | None = None


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
    """Return the current time in UTC (``domainkit.clock``)."""
    return clock.now()


def host_of(url: str) -> str:
    """The host of ``url``, lower case, without user, port, path, or query."""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:  # A URL stored before the guard checked it as now.
        return ""


def secret_box() -> SecretBox:
    """The box subscription secrets are encrypted in (needs ``SECRET_KEY``)."""
    return SecretBox(SECRET_PURPOSE)


class Subscription(AggregateRoot[SubscriptionId]):
    """A URL that receives the events it names, signed with its secret.

    Neither the URL nor the secret appears in its ``repr``: URLs may hold
    tokens (``https://hooks.example.com/in/T0K3N``).
    """

    url: str = field(repr=False)
    event_types: list[str]
    secret: bytes = field(repr=False)
    """The signing secret, encrypted and bound to the id (``secret_box``)."""
    active: bool = True
    description: str = ""
    previous_secret: bytes | None = field(default=None, repr=False)
    """The secret before the last rotation, encrypted; it signs until it expires."""
    previous_secret_expires_at: datetime | None = None
    created_at: datetime = field(default_factory=_now)
    owner: str | None = None
    """The subject whose events alone it receives; None for staff's, which
    receive every event they want."""

    @classmethod
    def create(
        cls,
        url: str,
        host: str,
        event_types: Iterable[str],
        description: str = "",
        *,
        active: bool = True,
        owner: str | None = None,
    ) -> tuple[Self, str]:
        """Subscribe ``url`` (checked by the caller) with a new secret.

        ``owner`` limits it to the events of one subject (see ``expose``).

        Returns:
            The subscription, and its secret: shown this once, stored encrypted.
        """
        if owner is not None and not 1 <= len(owner) <= MAX_OWNER:
            raise BusinessRuleViolation(
                f"A subscription's owner has 1 to {MAX_OWNER} characters."
            )
        subscription_id = SubscriptionId(uuid4())
        secret = new_secret()
        subscription = cls(
            id=subscription_id,
            url=url,
            event_types=checked_event_types(event_types),
            secret=secret_box().encrypt(
                secret.encode(), associated=subscription_id.bytes
            ),
            active=active,
            description=_description(description),
            owner=owner,
        )
        subscription.record(
            SubscriptionCreated(
                subscription_id=subscription.id,
                host=host,
                event_types=tuple(subscription.event_types),
                owner=owner,
            )
        )
        return subscription, secret

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
        host: str | None = None,
    ) -> None:
        """Change what is given; record which fields changed, if any.

        A new URL is recorded with its host and the previous one: ``host``
        when the caller checked it, else read from the URL.
        """
        changed = []
        new_host = previous_host = None
        if url is not None and url != self.url:
            new_host, previous_host = host or host_of(url), host_of(self.url)
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
                SubscriptionChanged(
                    subscription_id=self.id,
                    fields=tuple(changed),
                    host=new_host,
                    previous_host=previous_host,
                )
            )

    def rotate_secret(self, overlap: timedelta) -> str:
        """Make a new secret and return it; the old one signs for ``overlap`` more.

        The old secret is encrypted again, under the current key. One that no
        longer decrypts (``SECRET_KEY`` changed with no fallback) is dropped:
        nothing can sign with it.
        """
        box = secret_box()
        associated = self.id.bytes
        previous = None
        if overlap > timedelta(0):
            try:
                old, _ = box.decrypt(self.secret, associated=associated)
            except DecryptionFailed:
                pass  # Lost with its key.
            else:
                previous = box.encrypt(old, associated=associated)
        self.previous_secret = previous
        self.previous_secret_expires_at = None if previous is None else _now() + overlap
        secret = new_secret()
        self.secret = box.encrypt(secret.encode(), associated=associated)
        self.record(SubscriptionSecretRotated(subscription_id=self.id))
        return secret

    def rotating(self, now: datetime) -> bool:
        """Whether the previous secret still signs deliveries made at ``now``."""
        return previous_signs(
            self.previous_secret, self.previous_secret_expires_at, now
        )

    def delete(self) -> None:
        """Record the deletion (the repository removes the row)."""
        self.record(SubscriptionDeleted(subscription_id=self.id))


def previous_signs(
    previous: bytes | None, previous_expires_at: datetime | None, now: datetime
) -> bool:
    """Whether a previous secret signs, beside the current one, at ``now``."""
    return (
        previous is not None
        and previous_expires_at is not None
        and now < previous_expires_at
    )


def _description(text: str) -> str:
    """Check the length of a description."""
    if len(text) > MAX_DESCRIPTION:
        raise BusinessRuleViolation(
            f"A description has at most {MAX_DESCRIPTION} characters."
        )
    return text
