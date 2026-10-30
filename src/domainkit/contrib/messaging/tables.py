"""The outbox: one row per durable handler call or background task."""

from typing import Final

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Index,
    Integer,
    String,
    Table,
    Text,
    Uuid,
)

from domainkit.db import UTCDateTime, metadata

DEFAULT_QUEUE: Final = "default"
"""Where durable handlers, and tasks that name no queue, go."""
PENDING: Final = "pending"
HELD: Final = "held"
"""A message waiting for an earlier one of its ordering key to finish.

No worker claims it, and no relay publishes it: when the message before it
finishes, the worker that finished it makes the next one ``pending``."""
DONE: Final = "done"
DEAD: Final = "dead"
CANCELLED: Final = "cancelled"
STATUSES: Final = (PENDING, HELD, DONE, DEAD, CANCELLED)
FINISHED: Final = (DONE, CANCELLED)
"""The statuses cleanup deletes once ``RETENTION_SECONDS`` have passed."""
WAITING: Final = (PENDING, HELD)
"""The statuses of a key's messages that hold back the ones committed after."""
KEYED: Final = (PENDING, HELD, DEAD)
"""The statuses of the keyed messages ``ORDERING_INDEX`` holds."""
BLANKED: Final = "payload_blanked"
"""The header set on a dead message whose arguments were blanked (``sensitive``)."""
SENSITIVE: Final = "sensitive"
"""The header of a chain's step that follows a ``sensitive`` one, at any distance.

It is handed what the sensitive step had, or made, so its arguments are
blanked when it dies, as the sensitive step's would be."""

messages = Table(
    "domainkit_messages",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("target", String(255), nullable=False),
    Column("queue", String(64), nullable=False, server_default=DEFAULT_QUEUE),
    Column("priority", Integer, nullable=False, server_default="0"),
    Column("payload", JSON, nullable=False),
    Column("headers", JSON, nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("available_at", UTCDateTime, nullable=False),
    Column("locked_until", UTCDateTime),
    Column("lock_id", Uuid),
    Column("started_at", UTCDateTime),
    Column("created_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    Column("last_error", Text),
    Column("dedupe_key", String(255), unique=True),
    Column("ordering_key", String(255)),
    Column("sequence", BigInteger),
    Index("ix_domainkit_messages_ready", "status", "available_at"),
    info={"domain": "messaging"},
)
"""``kind`` is ``event`` (a durable handler) or ``task``; ``target`` names it.

``queue`` picks the workers that run it (``runworker --queue``), and
``priority`` the order among a queue's due messages (higher first).
``started_at`` is when a worker last started it; it is cleared when the
message goes back to wait. ``payload`` holds the arguments until the
message is done or cancelled, then ``{}``; a dead message keeps them, unless
its task is ``sensitive``. A pending message with ``started_at`` and a live
``locked_until`` is running; with a live lock alone, a relay published it.

Messages with an ``ordering_key`` run one at a time, in ``sequence``: the
order their key's commits took (``ordering_keys``). Only the first of a
key that has not finished is ``pending``; the ones after it are ``held``.
"""

PRIORITY_INDEX: Final = Index(
    "ix_domainkit_messages_priority",
    messages.c.status,
    messages.c.priority.desc(),
    messages.c.available_at,
)
"""Claims take due messages by priority, then by age."""

ORDERING_INDEX: Final = Index(
    "ix_domainkit_messages_ordering",
    messages.c.ordering_key,
    messages.c.sequence,
    postgresql_where=(
        messages.c.ordering_key.is_not(None) & messages.c.status.in_(KEYED)
    ),
    sqlite_where=(messages.c.ordering_key.is_not(None) & messages.c.status.in_(KEYED)),
)
"""A key's waiting and dead messages, in their order.

Partial: messages without a key, and finished ones, are not in it, so it
costs nothing to the rest of the outbox. As with ``FINISHED_INDEX``, a
statement must name the statuses it reads as literals, or the database
cannot tell that the index applies.
"""

ordering_keys = Table(
    "domainkit_ordering_keys",
    metadata,
    Column("key", String(255), primary_key=True),
    Column("sequence", BigInteger, nullable=False),
    Column("touched_at", UTCDateTime, nullable=False),
    info={"domain": "messaging", "fixtures": False},
)
"""The ordering keys of an outbox: each key's last ``sequence``, and its lock.

Every transaction that writes a message with a key, and every one that
finishes, cancels, or retries one, first writes the key's row here, so they
take their turns on it: a key's messages get their ``sequence`` in the
order their commits happen, and none decides that a message is first of
its key while another, uncommitted, says otherwise. Under ``REPEATABLE
READ`` or ``SERIALIZABLE``, a transaction whose snapshot predates another's
change to the row fails to write it, with a serialization failure that
``UnitOfWork.run`` retries, instead of deciding on what it saw. Cleanup
deletes a key's row once the key has no waiting or dead message and has
not been written for ``RETENTION_SECONDS``.
"""

inbox = Table(
    "domainkit_inbox",
    metadata,
    Column("target", String(255), primary_key=True),
    Column("message_id", Uuid, primary_key=True),
    Column("outbox", String(255), nullable=False),
    Column("handled_at", UTCDateTime, nullable=False),
    Index("ix_domainkit_inbox_outbox", "outbox"),
    info={"domain": "messaging", "fixtures": False},
)
"""The messages each ``inbox`` handler or task committed its work for.

One row per ``(target, message_id)``, written in the transaction of the
handler's own commit (``runs``), so the row and the work commit or roll back
together; a run of a message whose row exists is skipped. ``outbox`` is the
alias of the database whose outbox holds the message. Rows hold no
arguments, only the target's name, the message's id and outbox, and when it
was handled. Workers delete a row once its message finished: while it is
pending or dead it may run again, however long it waits.
"""

FINISHED_INDEX: Final = Index(
    "ix_domainkit_messages_finished",
    messages.c.finished_at,
    postgresql_where=messages.c.status.in_(FINISHED),
    sqlite_where=messages.c.status.in_(FINISHED),
)
"""Cleanup finds the oldest done and cancelled messages, and only those.

Partial: pending and dead messages are not in it, so it costs a write only
when a message finishes. A cleanup statement must name both statuses as
literals, or the database cannot tell that the index applies.
"""
