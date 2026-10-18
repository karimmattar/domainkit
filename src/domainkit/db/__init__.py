"""SQLAlchemy integration: connections, ORM metadata, repositories, migrations."""

from domainkit.db.connections import ConnectionHandler, connections, create_engine
from domainkit.db.orm import map_imperatively, mapper_registry, metadata
from domainkit.db.repository import SqlAlchemyRepository
from domainkit.db.types import ExactDecimal, UTCDateTime
from domainkit.db.uow import SqlAlchemyUnitOfWork

__all__ = [
    "ConnectionHandler",
    "ExactDecimal",
    "SqlAlchemyRepository",
    "SqlAlchemyUnitOfWork",
    "UTCDateTime",
    "connections",
    "create_engine",
    "map_imperatively",
    "mapper_registry",
    "metadata",
]
