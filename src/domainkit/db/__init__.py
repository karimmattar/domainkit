"""SQLAlchemy integration: connections, ORM metadata, repositories, migrations."""

from domainkit.db.connections import (
    ConnectionHandler,
    DatabaseAccessRefused,
    Reads,
    connections,
)
from domainkit.db.connections import (
    create_engine as create_engine,
)
from domainkit.db.derive import (
    EnumValue,
    ValueObjectType,
    register_column_type,
    table_for,
)
from domainkit.db.encrypted import EncryptedColumns, EncryptedType
from domainkit.db.money import MoneyColumns
from domainkit.db.orm import (
    child,
    children,
    map_imperatively,
    metadata,
)
from domainkit.db.orm import (
    mapper_registry as mapper_registry,
)
from domainkit.db.repository import SqlAlchemyRepository
from domainkit.db.types import CurrencyCode, ExactDecimal, UTCDateTime
from domainkit.db.uow import Isolation, SqlAlchemyUnitOfWork, lock_key

__all__ = [
    "ConnectionHandler",
    "CurrencyCode",
    "DatabaseAccessRefused",
    "EncryptedColumns",
    "EncryptedType",
    "EnumValue",
    "ExactDecimal",
    "Isolation",
    "MoneyColumns",
    "Reads",
    "SqlAlchemyRepository",
    "SqlAlchemyUnitOfWork",
    "UTCDateTime",
    "ValueObjectType",
    "child",
    "children",
    "connections",
    "lock_key",
    "map_imperatively",
    "metadata",
    "register_column_type",
    "table_for",
]
