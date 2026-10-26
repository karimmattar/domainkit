"""Signed checkpoints of the audit chain: where it may be pruned, what is exported.

A checkpoint says "the chain of database D (in namespace N) had link L at
seal S", signed with the project's Ed25519 key (``AUDIT.CHECKPOINT_KEY``).
Its document is canonical JSON, and the signature covers its exact bytes,
so any Ed25519 library can check it with the public key alone, without
the database, the ledger, or the operator's credentials. ADR 0082 fixes
the format::

    {"database": "default", "format": "domainkit.audit.checkpoint/1",
     "key_id": "3f2a9c...", "link": "<64 hex>", "made_at": "2026-...",
     "namespace": "acme-prod", "previous": {"link": "...", "seq": 1000},
     "sealed_at": "2026-...", "seq": 1234}

``previous`` is the checkpoint before it in the same chain (null for the
first), so a run of checkpoints is a chain too.

Two uses:

- ``auditprune`` stores one (``domainkit_audit_checkpoints``) and deletes
  the rows and seals below it; ``verify`` then starts the chain from the
  checkpoint instead of the genesis (``pruning``).
- ``auditverify --export`` writes the stored checkpoints and one of the
  current head to a file (``export``), which ``check_export`` (and
  ``auditverify --check-export``) checks offline.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.apps import registry
from domainkit.exceptions import DomainkitError, ImproperlyConfigured

from .settings import AuditSettings
from .tables import audit_checkpoints

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

__all__ = [
    "EXPORT_FORMAT",
    "FORMAT",
    "Checkpoint",
    "CheckpointInvalid",
    "check_export",
    "key_id",
    "load_public_key",
    "signing_key",
]

FORMAT: Final = "domainkit.audit.checkpoint/1"
"""What a checkpoint document says it is; another value is refused."""
EXPORT_FORMAT: Final = "domainkit.audit.export/1"
"""What an export says it is."""
_FIELDS: Final = frozenset(
    {
        "database",
        "format",
        "key_id",
        "link",
        "made_at",
        "namespace",
        "previous",
        "sealed_at",
        "seq",
    }
)
_HEX: Final = frozenset("0123456789abcdef")
SIGNATURE_BYTES: Final = 64


class CheckpointInvalid(ValueError, DomainkitError):
    """A checkpoint, or an export, that does not verify: never trust it."""


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """The chain of ``database`` had ``link`` at seal ``seq``, as signed."""

    database: str
    namespace: str | None
    seq: int
    link: str
    sealed_at: datetime
    """When seal ``seq`` was made."""
    made_at: datetime
    previous: tuple[int, str] | None
    """The checkpoint before this one in the chain: its seal and link."""
    key_id: str
    """Which key signed it (``key_id``)."""

    def document(self) -> str:
        """The canonical JSON the signature covers: sorted keys, no spaces."""
        previous = (
            None
            if self.previous is None
            else {"seq": self.previous[0], "link": self.previous[1]}
        )
        return json.dumps(
            {
                "format": FORMAT,
                "database": self.database,
                "namespace": self.namespace,
                "seq": self.seq,
                "link": self.link,
                "sealed_at": self.sealed_at.isoformat(),
                "made_at": self.made_at.isoformat(),
                "previous": previous,
                "key_id": self.key_id,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @classmethod
    def parse(cls, document: str) -> Checkpoint:
        """Read a document back, checking every field.

        Raises:
            CheckpointInvalid: It is not a checkpoint in this format, or not
                written canonically (then its bytes are not the ones signed).
        """
        try:
            found = json.loads(document)
            if not isinstance(found, dict) or found.keys() != _FIELDS:
                raise CheckpointInvalid("A checkpoint has exactly its fields.")
            if found["format"] != FORMAT:
                raise CheckpointInvalid(f"Not a checkpoint of format {FORMAT}.")
            previous = found["previous"]
            checkpoint = cls(
                database=_text(found["database"]),
                namespace=None
                if found["namespace"] is None
                else _text(found["namespace"]),
                seq=_count(found["seq"]),
                link=_digest(found["link"]),
                sealed_at=datetime.fromisoformat(_text(found["sealed_at"])),
                made_at=datetime.fromisoformat(_text(found["made_at"])),
                previous=None
                if previous is None
                else (_count(previous["seq"]), _digest(previous["link"])),
                key_id=_text(found["key_id"]),
            )
        except CheckpointInvalid:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise CheckpointInvalid("Not a checkpoint document.") from exc
        if checkpoint.document() != document:
            raise CheckpointInvalid("A checkpoint document is written canonically.")
        if checkpoint.previous is not None and checkpoint.previous[0] >= checkpoint.seq:
            raise CheckpointInvalid("A checkpoint follows the one before it.")
        return checkpoint


def _text(value: object) -> str:
    """``value`` if it is text."""
    if not isinstance(value, str):
        raise CheckpointInvalid("Expected text.")
    return value


def _count(value: object) -> int:
    """``value`` if it is a seal's number."""
    if type(value) is not int or value < 1:
        raise CheckpointInvalid("Expected a seal number.")
    return value


def _digest(value: object) -> str:
    """``value`` if it is a link: 64 lower-case hex digits."""
    text = _text(value)
    if len(text) != 64 or not set(text) <= _HEX:
        raise CheckpointInvalid("Expected a link.")
    return text


def signing_key() -> Ed25519PrivateKey:
    """The key ``AUDIT.CHECKPOINT_KEY`` holds.

    Raises:
        ImproperlyConfigured: It is not set, cryptography is missing, or it
            is not an Ed25519 private key in PEM.
    """
    config = registry.settings(AuditSettings)
    if config.CHECKPOINT_KEY is None:
        raise ImproperlyConfigured(
            "Checkpoints are signed with AUDIT.CHECKPOINT_KEY, which is not set: "
            "set it to an Ed25519 private key in PEM "
            "(openssl genpkey -algorithm ed25519)."
        )
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError as exc:
        raise ImproperlyConfigured(
            'Signed checkpoints need cryptography: pip install "domainkit[audit]".'
        ) from exc
    try:
        key = load_pem_private_key(
            config.CHECKPOINT_KEY.get_secret_value().encode(), password=None
        )
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            "AUDIT.CHECKPOINT_KEY is not a private key in PEM without a password."
        ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ImproperlyConfigured("AUDIT.CHECKPOINT_KEY must be an Ed25519 key.")
    return key


def load_public_key(pem: bytes) -> Ed25519PublicKey:
    """The Ed25519 public key in ``pem``, as a verifier pins it.

    Raises:
        CheckpointInvalid: It is not one.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    try:
        key = load_pem_public_key(pem)
    except ValueError as exc:
        raise CheckpointInvalid("Not a public key in PEM.") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise CheckpointInvalid("Checkpoints are signed with Ed25519 keys.")
    return key


def key_id(public_key: Ed25519PublicKey) -> str:
    """A short name of ``public_key``: the first 16 hex digits of its SHA-256."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def public_pem(public_key: Ed25519PublicKey) -> str:
    """``public_key`` in PEM, as an export carries it."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return public_key.public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    ).decode()


def sign(checkpoint: Checkpoint, key: Ed25519PrivateKey) -> bytes:
    """The signature of ``checkpoint``'s document under ``key``."""
    return key.sign(checkpoint.document().encode())


def verified(
    document: str, signature: bytes, public_key: Ed25519PublicKey
) -> Checkpoint:
    """The checkpoint ``document`` holds, if ``signature`` is its, under ``public_key``.

    Raises:
        CheckpointInvalid: The signature does not verify, another key made
            it, or the document is not a checkpoint.
    """
    from cryptography.exceptions import InvalidSignature

    try:
        public_key.verify(signature, document.encode())
    except InvalidSignature as exc:
        raise CheckpointInvalid("A checkpoint's signature does not verify.") from exc
    checkpoint = Checkpoint.parse(document)
    if checkpoint.key_id != key_id(public_key):
        raise CheckpointInvalid("A checkpoint names another key.")
    return checkpoint


@dataclass(frozen=True, slots=True)
class Stored:
    """A row of ``domainkit_audit_checkpoints``, verified or not."""

    seq: int
    document: str
    signature: bytes
    checkpoint: Checkpoint | None
    """None when it does not verify (forged, copied, or another key's)."""


async def stored(
    connection: AsyncConnection,
    database: str,
    public_key: Ed25519PublicKey | None,
) -> list[Stored]:
    """The checkpoints stored in ``database``'s trail, oldest first, each checked.

    One verifies only under ``public_key`` (none: none verifies), for this
    database and namespace, at the seal its row names.
    """
    namespace = registry.settings(AuditSettings).NAMESPACE
    rows = (
        await connection.execute(
            select(audit_checkpoints).order_by(audit_checkpoints.c.seq)
        )
    ).all()
    found = []
    for row in rows:
        checkpoint = None
        if public_key is not None:
            try:
                checkpoint = verified(row.document, bytes(row.signature), public_key)
            except CheckpointInvalid:
                checkpoint = None
            if checkpoint is not None and (
                checkpoint.database != database
                or checkpoint.namespace != namespace
                or checkpoint.seq != row.seq
            ):
                checkpoint = None
        found.append(Stored(row.seq, row.document, bytes(row.signature), checkpoint))
    return found


def configured_public_key() -> Ed25519PublicKey | None:
    """The public half of ``AUDIT.CHECKPOINT_KEY``; None when it is not set."""
    if registry.settings(AuditSettings).CHECKPOINT_KEY is None:
        return None
    return signing_key().public_key()


def signed(checkpoint: Checkpoint, signature: bytes) -> dict[str, str]:
    """A checkpoint as an export holds it: its exact document, and its signature."""
    return {
        "document": checkpoint.document(),
        "signature": base64.b64encode(signature).decode(),
    }


def check_export(data: bytes, public_key_pem: bytes) -> dict[str, list[Checkpoint]]:
    """Check an export offline, with the public key alone; its checkpoints by database.

    Each checkpoint's signature must verify under ``public_key_pem`` (the
    export's own key is not trusted, only compared), name that key, the
    database it is filed under, and the export's namespace, and follow
    the one before it (``previous``), seal numbers rising.

    Raises:
        CheckpointInvalid: Anything does not.
    """
    public_key = load_public_key(public_key_pem)
    try:
        export: Any = json.loads(data)
        if export["format"] != EXPORT_FORMAT:
            raise CheckpointInvalid(f"Not an export of format {EXPORT_FORMAT}.")
        databases: Mapping[str, Any] = export["databases"]
        namespace = export["namespace"]
        entries = {
            database: [
                (
                    _text(entry["document"]),
                    base64.b64decode(entry["signature"], validate=True),
                )
                for entry in found["checkpoints"]
            ]
            for database, found in databases.items()
        }
    except CheckpointInvalid:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise CheckpointInvalid("Not an export.") from exc
    checked: dict[str, list[Checkpoint]] = {}
    for database, pairs in entries.items():
        chain: list[Checkpoint] = []
        for document, signature in pairs:
            checkpoint = verified(document, signature, public_key)
            if (checkpoint.database, checkpoint.namespace) != (database, namespace):
                raise CheckpointInvalid(
                    f"A checkpoint filed under {database!r} is another chain's."
                )
            expected = None if not chain else (chain[-1].seq, chain[-1].link)
            if checkpoint.previous != expected:
                raise CheckpointInvalid(
                    f"Checkpoint {checkpoint.seq} of {database!r} does not follow "
                    "the one before it."
                )
            chain.append(checkpoint)
        checked[database] = chain
    return checked
