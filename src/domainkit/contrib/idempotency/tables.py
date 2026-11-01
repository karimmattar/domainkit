"""Stored responses, keyed by a hash of the caller and the key."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    text,
)

from domainkit.db import UTCDateTime, metadata

idempotency_keys = Table(
    "domainkit_idempotency_keys",
    metadata,
    Column("key_hash", String(64), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("status_code", Integer),
    Column("headers", JSON),
    Column("body", LargeBinary),
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False, index=True),
    Column("committed_at", UTCDateTime),
    Column("token", String(32), nullable=False),
    Column("locked_until", UTCDateTime, nullable=False),
    Column("pinned", Boolean, nullable=False, server_default=text("false")),
    Column("subjects", Text),
    Column("tombstone", Boolean, nullable=False, server_default=text("false")),
    info={"domain": "idempotency"},
)
"""A row without ``status_code`` is a request still in progress.

``committed_at`` is set once the request's changes commit. A finished row
whose ``body`` is NULL ran, but its response was not kept. ``token`` names
the request holding the key; a retry may take over a key whose
``locked_until`` has passed, unless its changes committed. ``pinned`` is
set before changes on another database commit: such a key is never taken
over or released, since those changes may have committed whatever their
commit reported. ``subjects`` names the people a kept response may be
about (``domainkit.ddd.personal.subject_tag``): the actor, and the subjects
of the ``Personal`` fields of the events its request committed. Erasing
one of them drops the kept response (``operations.erase_subject``).

``expires_at`` is when the row may be replaced or purged. Once it passes,
a row whose request committed or pinned the key is not: the purge makes it
a ``tombstone`` instead, dropping its response, headers and ``subjects``,
and moves ``expires_at`` on by ``TOMBSTONE_SECONDS``. Until then a retry
gets 409 rather than running again (FIN-84).
"""
