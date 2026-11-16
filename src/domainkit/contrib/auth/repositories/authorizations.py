"""Acts waiting for their user's authorization, or holding it (``auth.actions``)."""

import dataclasses
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..tables import authorizations
from ..tokens import utc_now
from ._shared import changed_rows

TITLE_MAX_LENGTH: Final = 64
"""The longest title an act shows."""
DETAIL_MAX_LENGTH: Final = 64
"""The longest label, and the longest value, an act shows."""
MAX_DETAILS: Final = 6
"""The most labelled values an act shows."""


@dataclass(frozen=True, slots=True, init=False)
class Shown:
    """What the user is shown of an act: a title, and up to six labelled values.

    ``Shown("End a membership", {"Member": "Alice Smith"})``: the title, at
    most 64 printable characters, and at most six labels and values of at
    most 64 each, in order. The server builds it, and every screen shows
    the words kept with the authorization, never words a page sent.
    """

    title: str
    details: tuple[tuple[str, str], ...]
    """The labels and their values, in the order given."""

    def __init__(
        self, title: str, details: Mapping[str, str] | Iterable[tuple[str, str]] = ()
    ) -> None:
        """Keep ``title`` and ``details``, if a phone's screen can show them.

        Raises:
            ValueError: The title is empty or too long; there are more than
                six details, or a label twice; a label is empty, or a label
                or value is too long; or any holds control characters.
        """
        pairs = tuple(
            (str(label), str(value))
            for label, value in (
                details.items() if isinstance(details, Mapping) else details
            )
        )
        _check_text(title, "A shown title", empty=False)
        if len(pairs) > MAX_DETAILS:
            raise ValueError(f"An act shows {MAX_DETAILS} details at most.")
        if len({label for label, _ in pairs}) != len(pairs):
            raise ValueError("An act shows each label once.")
        for label, value in pairs:
            _check_text(label, "A shown label", empty=False)
            _check_text(value, "A shown value", empty=True)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "details", pairs)

    def as_json(self) -> dict[str, Any]:
        """``{"title": ..., "details": [[label, value], ...]}``, as stored."""
        return {"title": self.title, "details": [list(pair) for pair in self.details]}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Shown":
        """What ``as_json`` stored."""
        return cls(data["title"], [(label, value) for label, value in data["details"]])

    def digest(self) -> str:
        """The SHA-256 of what is shown, as canonical JSON (64 hex digits)."""
        text = json.dumps(
            self.as_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_text(text: str, what: str, *, empty: bool) -> None:
    """Refuse ``text`` a phone's screen cannot show whole.

    Raises:
        ValueError: It is not text, is empty (unless ``empty``), longer than
            64 characters, or holds control characters.
    """
    limit = TITLE_MAX_LENGTH if what.endswith("title") else DETAIL_MAX_LENGTH
    if (
        not isinstance(text, str)
        or len(text) > limit
        or (not empty and not text)
        or not text.isprintable()
    ):
        least = 0 if empty else 1
        raise ValueError(f"{what} is {least} to {limit} printable characters.")


@dataclass(frozen=True, slots=True)
class ActionAuthorization:
    """An act waiting for its user's authorization, or holding it (ADR 0144).

    Its ``repr`` names its id, purpose, subject, and state: what is shown
    may be personal data, and the login's digest a lookup key.
    """

    id: UUID
    user_id: UUID
    login: str = dataclasses.field(repr=False)
    """The digest of the login that asked: only it spends the authorization."""
    purpose: str
    subject: str
    """What the act is on: ``"<type>:<id>"``, an internal id."""
    digest: str
    """The act's fingerprint: the purpose and the command's arguments."""
    shown: Shown = dataclasses.field(repr=False)
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None = None
    amr: str | None = None
    """What confirmed it: RFC 8176's names, and ``phone``, separated by spaces."""
    consumed_at: datetime | None = None
    """When it was spent, voided, or ended: it never works again."""
    device_id: UUID | None = None
    """The bound device that confirmed it, if one did."""
    algorithm: int | None = None
    """The COSE algorithm of the device's key (``-7``: ES256, ``-8``: EdDSA)."""
    public_key: bytes | None = dataclasses.field(default=None, repr=False)
    """The device's public key, kept as long as the proof."""
    signature: bytes | None = dataclasses.field(default=None, repr=False)
    signed: bytes | None = dataclasses.field(default=None, repr=False)
    """What the device signed, whole: with ``public_key``, the signature
    can be checked again once the device is unbound."""
    signed_at: datetime | None = None
    sign_in_request_id: UUID | None = None
    """The phone's sign-in request that confirms it (``domainkit.contrib.ciba``)."""
    passkey_challenge: str | None = dataclasses.field(default=None, repr=False)
    """The passkey's challenge made for it (``domainkit.contrib.devices``)."""

    @property
    def methods(self) -> frozenset[str]:
        """What confirmed it; empty while unconfirmed."""
        return frozenset((self.amr or "").split())

    def expired(self, now: datetime | None = None) -> bool:
        """Whether it no longer works."""
        return self.expires_at <= (now or utc_now())


class AuthorizationStore:
    """Action authorizations, by id (``auth.actions``)."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def add(self, authorization: ActionAuthorization) -> None:
        """Store ``authorization``, waiting for its confirmation."""
        values = {
            field.name: getattr(authorization, field.name)
            for field in dataclasses.fields(authorization)
        }
        values["shown"] = authorization.shown.as_json()
        await self.session.execute(insert(authorizations).values(**values))

    async def get(self, authorization_id: UUID) -> ActionAuthorization | None:
        """The authorization ``authorization_id``, in any state."""
        row = (
            await self.session.execute(
                select(authorizations).where(authorizations.c.id == authorization_id)
            )
        ).first()
        return None if row is None else _loaded(dict(row._mapping))

    async def linked(
        self, user_id: UUID, sign_in_request_id: UUID
    ) -> ActionAuthorization | None:
        """The user's authorization the phone's request confirms, linked to it."""
        row = (
            await self.session.execute(
                select(authorizations)
                .where(
                    authorizations.c.user_id == user_id,
                    authorizations.c.sign_in_request_id == sign_in_request_id,
                )
                .limit(1)
            )
        ).first()
        return None if row is None else _loaded(dict(row._mapping))

    async def end_unconfirmed(self, login: str, *, keep: int, now: datetime) -> int:
        """End the login's live, unconfirmed authorizations but its ``keep`` newest.

        How many it ended.
        """
        newest = (
            select(authorizations.c.id)
            .where(*self._waiting(now), authorizations.c.login == login)
            .order_by(authorizations.c.created_at.desc(), authorizations.c.id.desc())
            .offset(keep)
        )
        ended = list(await self.session.scalars(newest))
        if not ended:
            return 0
        result = await self.session.execute(
            update(authorizations)
            .where(
                authorizations.c.id.in_(ended), authorizations.c.consumed_at.is_(None)
            )
            .values(consumed_at=now)
        )
        return changed_rows(result)

    async def confirm(
        self,
        authorization_id: UUID,
        when: datetime,
        methods: Iterable[str],
        *,
        device_id: UUID | None = None,
        algorithm: int | None = None,
        public_key: bytes | None = None,
        signature: bytes | None = None,
        signed: bytes | None = None,
        signed_at: datetime | None = None,
    ) -> bool:
        """Mark it confirmed at ``when`` by ``methods``, if it waits still.

        Unconfirmed, unspent, and live; the device's proof is kept with it.
        """
        result = await self.session.execute(
            update(authorizations)
            .where(authorizations.c.id == authorization_id, *self._waiting(when))
            .values(
                confirmed_at=when,
                amr=" ".join(sorted(set(methods))),
                device_id=device_id,
                algorithm=algorithm,
                public_key=public_key,
                signature=signature,
                signed=signed,
                signed_at=signed_at,
            )
        )
        return changed_rows(result) == 1

    async def consume(self, authorization_id: UUID, when: datetime) -> bool:
        """Spend it at ``when``, if it is confirmed, unspent, and live: once."""
        result = await self.session.execute(
            update(authorizations)
            .where(
                authorizations.c.id == authorization_id,
                authorizations.c.confirmed_at.is_not(None),
                authorizations.c.consumed_at.is_(None),
                authorizations.c.expires_at > when,
            )
            .values(consumed_at=when)
        )
        return changed_rows(result) == 1

    async def void(self, authorization_id: UUID, when: datetime) -> bool:
        """End it at ``when``, confirmed or not, unless it was spent already."""
        result = await self.session.execute(
            update(authorizations)
            .where(
                authorizations.c.id == authorization_id,
                authorizations.c.consumed_at.is_(None),
            )
            .values(consumed_at=when)
        )
        return changed_rows(result) == 1

    async def link(
        self,
        authorization_id: UUID,
        when: datetime,
        *,
        sign_in_request_id: UUID | None = None,
        passkey_challenge: str | None = None,
    ) -> bool:
        """Link what will confirm it, if it waits still; links not given stay."""
        values: dict[str, object] = {}
        if sign_in_request_id is not None:
            values["sign_in_request_id"] = sign_in_request_id
        if passkey_challenge is not None:
            values["passkey_challenge"] = passkey_challenge
        if not values:
            return False
        result = await self.session.execute(
            update(authorizations)
            .where(authorizations.c.id == authorization_id, *self._waiting(when))
            .values(**values)
        )
        return changed_rows(result) == 1

    async def of_user(self, user_id: UUID) -> list[ActionAuthorization]:
        """The user's authorizations, oldest first."""
        rows = await self.session.execute(
            select(authorizations)
            .where(authorizations.c.user_id == user_id)
            .order_by(authorizations.c.created_at, authorizations.c.id)
        )
        return [_loaded(dict(row._mapping)) for row in rows]

    async def clear_expired(self, now: datetime | None = None) -> int:
        """Delete authorizations that no longer work; how many."""
        result = await self.session.execute(
            delete(authorizations).where(
                authorizations.c.expires_at <= (now or utc_now())
            )
        )
        return changed_rows(result)

    @staticmethod
    def _waiting(when: datetime) -> tuple[Any, ...]:
        """The conditions of one that waits: unconfirmed, unspent, live at ``when``."""
        return (
            authorizations.c.confirmed_at.is_(None),
            authorizations.c.consumed_at.is_(None),
            authorizations.c.expires_at > when,
        )


def _loaded(values: dict[str, Any]) -> ActionAuthorization:
    """The authorization a row of ``domainkit_authorizations`` holds."""
    return ActionAuthorization(shown=Shown.from_json(values.pop("shown")), **values)
