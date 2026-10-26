"""SQLAlchemy integration: connections, ORM metadata, repositories, migrations."""

from domainkit.db.connections import (
    ConnectionHandler,
    Reads,
    connections,
    create_engine,
)
from domainkit.db.derive import (
    EnumValue,
    ValueObjectType,
    register_column_type,
    table_for,
)
from domainkit.db.orm import (
    child,
    children,
    map_imperatively,
    mapper_registry,
    metadata,
)
from domainkit.db.repository import SqlAlchemyRepository
from domainkit.db.types import ExactDecimal, UTCDateTime
from domainkit.db.uow import Isolation, SqlAlchemyUnitOfWork, lock_key

__all__ = [
    "ConnectionHandler",
    "EnumValue",
    "ExactDecimal",
    "Isolation",
    "Reads",
    "SqlAlchemyRepository",
    "SqlAlchemyUnitOfWork",
    "UTCDateTime",
    "ValueObjectType",
    "child",
    "children",
    "connections",
    "create_engine",
    "lock_key",
    "map_imperatively",
    "mapper_registry",
    "metadata",
    "register_column_type",
    "table_for",
]
