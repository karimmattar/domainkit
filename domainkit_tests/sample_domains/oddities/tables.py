"""Tables of the oddities domain."""

from sqlalchemy import JSON, Column, Integer, LargeBinary, String, Table, Uuid

from domainkit.db import map_imperatively, metadata

from .models import Basket, Memo, Note, Secret, Ticket

notes = Table(
    "oddities_notes",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("author_id", Uuid, nullable=False),
    Column("text", String(100), nullable=False),
    Column("scan", LargeBinary, nullable=False),
    info={"domain": "oddities"},
)

memos = Table(
    "oddities_memos",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("author_id", Uuid, nullable=False),
    info={"domain": "oddities"},
)

tickets = Table(
    "oddities_tickets",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("owner", String(40), nullable=False),
    Column("code", String(8), nullable=False, unique=True),
    info={"domain": "oddities"},
)

baskets = Table(
    "oddities_baskets",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("lines", JSON, nullable=False),
    info={"domain": "oddities"},
)

secrets = Table(
    "oddities_secrets",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("owner", String(40), nullable=False),
    info={"domain": "oddities"},
)


def start_mappers() -> None:
    """Map the aggregates, without versions: odd tables, on purpose."""
    map_imperatively(Note, notes, versioned=False)
    map_imperatively(Memo, memos, versioned=False)
    map_imperatively(Ticket, tickets, versioned=False)
    map_imperatively(Basket, baskets, versioned=False)
    map_imperatively(Secret, secrets, versioned=False)
