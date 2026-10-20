"""Flags and stored dynamic settings, as aggregates.

A flag is on for a user when ``everyone`` is True; off for all when it is
False; otherwise (None) when the user is listed in ``users``, or is in one
of ``groups``, or falls in the ``percentage`` rollout. The rollout hashes
the flag's name with the user's id, so a user stays in or out while the
percentage stays the same, and stays in as it grows.
"""

import hashlib
from collections.abc import Iterable
from typing import Final

from domainkit.ddd import AggregateRoot, BusinessRuleViolation

from .events import FlagChanged, FlagDeleted, SettingChanged, SettingReset

BUCKETS: Final = 100
"""A percentage rollout puts each user in one of this many buckets."""


class InvalidFlag(BusinessRuleViolation):
    """A flag's rules are out of range."""


def bucket(name: str, user_id: str) -> int:
    """The user's bucket for flag ``name``: 0 to 99, the same in every process."""
    digest = hashlib.sha256(f"{name}\0{user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % BUCKETS


class Flag(AggregateRoot[str]):
    """A feature switched on for everyone, no one, or some users; ``id`` is its name."""

    everyone: bool | None = None
    percentage: int = 0
    users: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    note: str = ""

    class Meta:
        """Django's four permissions: view, add, change, delete."""

        verbose_name = "flag"

    def configure(
        self,
        *,
        everyone: bool | None,
        percentage: int,
        users: Iterable[str],
        groups: Iterable[str],
        note: str,
    ) -> None:
        """Set the flag's rules and record the change.

        Raises:
            InvalidFlag: ``percentage`` is outside 0 to 100.
        """
        if not 0 <= percentage <= BUCKETS:
            raise InvalidFlag("A rollout percentage is 0 to 100.")
        self.everyone = everyone
        self.percentage = percentage
        self.users = tuple(sorted(set(users)))
        self.groups = tuple(sorted(set(groups)))
        self.note = note
        self.record(
            FlagChanged(
                name=self.id,
                everyone=everyone,
                percentage=percentage,
                users=self.users,
                groups=self.groups,
            )
        )

    def delete(self) -> None:
        """Record that the flag is deleted."""
        self.record(FlagDeleted(name=self.id))

    def enabled_for(self, user_id: str | None, groups: Iterable[str]) -> bool:
        """Whether the flag is on for the user ``user_id`` in ``groups``.

        Anonymous callers (``user_id`` None) get it only with ``everyone``.
        """
        if self.everyone is not None:
            return self.everyone
        if user_id is None:
            return False
        if user_id in self.users or not set(self.groups).isdisjoint(groups):
            return True
        return bucket(self.id, user_id) < self.percentage


class DynamicSetting(AggregateRoot[str]):
    """The stored value of a dynamic setting; ``id`` is its key."""

    value: object = None

    class Meta:
        """Settings are declared in code: they are only viewed and changed."""

        verbose_name = "dynamic setting"
        default_permissions = ("change", "view")

    def change(self, value: object) -> None:
        """Store ``value`` (already validated, as JSON) and record it."""
        self.value = value
        self.record(SettingChanged(key=self.id, value=value))

    def reset(self) -> None:
        """Record that the setting goes back to its default."""
        self.record(SettingReset(key=self.id))
