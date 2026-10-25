"""Evidence: facts kept provably unchanged, and the receipts that prove them.

A record is stored as canonical JSON (sorted keys, no spaces, UTF-8). Its
SHA-256 digest is the receipt anyone holding a copy can check it against.
Amounts must be exact: ``Decimal`` facts are written as fixed-point strings
("10.00"), and floats are refused, since most decimal amounts have no exact
float and the receipt would prove a rounded value.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from domainkit import clock
from domainkit.context import current_request
from domainkit.ddd import Conflict, NotFound
from domainkit.utils.decimals import decimal_text

KIND: Final = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
"""Dotted lowercase names, such as ``kyc.document_verified``."""
DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")
"""How artifacts and records are referenced: ``sha256:<64 hex digits>``."""
FORMAT: Final = 1
"""Version of the record layout, stored in every record."""
SUBJECT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*")
"""Internal ids such as ``customer:42``: no ``@``, spaces, or ``+``."""
_KIND_LENGTH: Final = 100
_SUBJECT_LENGTH: Final = 255
_CANONICAL_JSON: Final = json.JSONEncoder(
    sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
)
"""Sorted keys, no spaces, UTF-8 text, and no NaN: one form per value."""


class EvidenceNotFound(NotFound):
    """No evidence is stored under that key."""


class EvidenceConflict(Conflict):
    """A different record already uses that evidence id."""


class EvidenceTampered(Exception):
    """The ledger copy does not match its receipt, or was written again."""


def _actor() -> str | None:
    """The actor of the current request, if any."""
    request = current_request()
    return request.actor if request else None


def _request_id() -> str | None:
    """The id of the current request, if any."""
    request = current_request()
    return request.request_id if request else None


def _exact(value: Any, path: str) -> Any:  # noqa: ANN401 - JSON values.
    """``value`` with Decimals as fixed-point strings; floats are refused.

    ``path`` names the place in the facts, never the value, which may be
    personal data.
    """
    if isinstance(value, float):
        raise ValueError(
            f"Evidence facts may not hold floats ({path}): use Decimal or str, "
            "so amounts stay exact."
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"Evidence facts must be finite numbers ({path}).")
        return decimal_text(value)
    if isinstance(value, Mapping):
        return {key: _exact(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_exact(item, f"{path}[{index}]") for index, item in enumerate(value)]
    return value


def sha256(data: bytes) -> str:
    """``sha256:<hex>`` of ``data``: how evidence refers to artifacts and records."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


@dataclass(frozen=True, slots=True)
class Evidence:
    """What happened, about whom, and the digests that back it up.

    Keep personal data out of ``subject`` and ``facts``: a ledger never
    forgets. The subject is an internal id, never an email address, name, or
    phone number. In facts, store references or digests, or encrypt personal
    fields with keys you can destroy. The current request's actor and id are
    captured when created.
    """

    kind: str
    """Dotted lowercase name, such as ``kyc.document_verified``."""
    subject: str
    """The internal id of what the evidence is about, such as ``customer:42``."""
    facts: Mapping[str, Any] = field(default_factory=dict)
    """JSON values: results, scores, versions, references.

    ``Decimal`` amounts are stored as strings ("10.00") and read back as
    strings; floats are refused.
    """
    artifacts: Mapping[str, str] = field(default_factory=dict)
    """Names of external documents to their ``sha256:`` digests."""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    recorded_at: datetime = field(default_factory=clock.now)
    actor: str | None = field(default_factory=_actor)
    request_id: str | None = field(default_factory=_request_id)
    _record: bytes | None = field(default=None, init=False, repr=False, compare=False)
    """The bytes ``from_canonical`` read, written back unchanged."""

    def __post_init__(self) -> None:
        """Refuse records that could not be stored or verified later."""
        if len(self.kind) > _KIND_LENGTH or not KIND.fullmatch(self.kind):
            raise ValueError(
                f"Evidence kind {self.kind!r} must be a dotted lowercase name."
            )
        if not 0 < len(self.subject) <= _SUBJECT_LENGTH:
            raise ValueError("Evidence subject must be 1 to 255 characters.")
        # The subject is not echoed: it may be the personal data refused here.
        if not SUBJECT.fullmatch(self.subject):
            raise ValueError(
                "Evidence subject must be an internal id such as 'customer:42' "
                "(letters, digits, and _ . : / -), never personal data such as "
                "an email address, a name, or a phone number."
            )
        for name, digest in self.artifacts.items():
            if not DIGEST.fullmatch(digest):
                raise ValueError(f"Artifact {name!r} must be a sha256:<hex> digest.")
        if self.recorded_at.tzinfo is None:
            raise ValueError("Evidence recorded_at must be timezone-aware.")

    def canonical(self) -> bytes:
        """The stored form: canonical JSON, the same bytes every time.

        Evidence read with ``from_canonical`` keeps the bytes it was read
        from, so a record written before floats were refused (and queued in
        the outbox) is still recorded exactly as its receipt says.
        """
        if self._record is not None:
            return self._record
        document = {
            "format": FORMAT,
            "id": str(self.id),
            "kind": self.kind,
            "subject": self.subject,
            "facts": _exact(self.facts, "facts"),
            "artifacts": dict(self.artifacts),
            "recorded_at": self.recorded_at.astimezone(UTC).isoformat(
                timespec="microseconds"
            ),
            "actor": self.actor,
            "request_id": self.request_id,
        }
        try:
            return _CANONICAL_JSON.encode(document).encode()
        except (TypeError, ValueError) as exc:  # UnicodeEncodeError: surrogates.
            raise ValueError(
                f"Evidence facts must be plain JSON values: {exc}"
            ) from exc

    @classmethod
    def from_canonical(cls, data: bytes) -> "Evidence":
        """Rebuild the evidence a canonical record describes."""
        document = json.loads(data)
        if document.get("format") != FORMAT:
            raise ValueError(f"Unknown evidence format {document.get('format')!r}.")
        evidence = cls(
            kind=document["kind"],
            subject=document["subject"],
            facts=document["facts"],
            artifacts=document["artifacts"],
            id=uuid.UUID(document["id"]),
            recorded_at=datetime.fromisoformat(document["recorded_at"]),
            actor=document["actor"],
            request_id=document["request_id"],
        )
        object.__setattr__(evidence, "_record", bytes(data))
        return evidence


@dataclass(frozen=True, slots=True)
class EvidenceReceipt:
    """Proof of where a record is and what it says; keep it with your data."""

    id: uuid.UUID
    key: str
    """Ledger key: ``<namespace>/<subject digest>/<evidence id>``."""
    digest: str
    """``sha256:`` of the canonical record."""
    transaction: int
    """Ledger transaction that stored it."""


@dataclass(frozen=True, slots=True)
class RecordedEvidence:
    """Evidence read back from the ledger, with its receipt."""

    evidence: Evidence
    receipt: EvidenceReceipt
    revisions: int
    """Writes to its key; anything but 1 means someone wrote it again."""

    @property
    def intact(self) -> bool:
        """Whether the key was written exactly once."""
        return self.revisions == 1
