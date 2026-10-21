"""Domain-driven design building blocks ("seedwork").

This package depends only on the standard library, ``domainkit.utils``, and
``domainkit.clock``, so a domain layer that imports nothing but
``domainkit.ddd`` stays free of frameworks and I/O.
"""

from domainkit.ddd.events import (
    DomainEvent,
    EventBus,
    EventHandler,
    HandlerTimedOut,
    event_bus,
)
from domainkit.ddd.exceptions import (
    BusinessRuleViolation,
    ConcurrentUpdate,
    Conflict,
    DomainError,
    DuplicateValue,
    NotFound,
    PermissionDenied,
    PreconditionFailed,
)
from domainkit.ddd.fields import (
    Digits,
    InvalidQuery,
    MaxLength,
    Reference,
    UnknownField,
)
from domainkit.ddd.messages import UNSET, Command, Query, Unset
from domainkit.ddd.model import AggregateRoot, Entity, ValueObject
from domainkit.ddd.pagination import Page
from domainkit.ddd.personal import Personal
from domainkit.ddd.query import Criteria, F, Ordering, RowLock, Specification
from domainkit.ddd.repository import (
    InMemoryRepository,
    LockNotAvailable,
    Repository,
    RowLocks,
    SoftDeletable,
)
from domainkit.ddd.uow import DeferredTask, ReadOnlyError, UnitOfWork

__all__ = [
    "UNSET",
    "AggregateRoot",
    "BusinessRuleViolation",
    "Command",
    "ConcurrentUpdate",
    "Conflict",
    "Criteria",
    "DeferredTask",
    "Digits",
    "DomainError",
    "DomainEvent",
    "DuplicateValue",
    "Entity",
    "EventBus",
    "EventHandler",
    "F",
    "HandlerTimedOut",
    "InMemoryRepository",
    "InvalidQuery",
    "LockNotAvailable",
    "MaxLength",
    "NotFound",
    "Ordering",
    "Page",
    "PermissionDenied",
    "Personal",
    "PreconditionFailed",
    "Query",
    "ReadOnlyError",
    "Reference",
    "Repository",
    "RowLock",
    "RowLocks",
    "SoftDeletable",
    "Specification",
    "UnitOfWork",
    "UnknownField",
    "Unset",
    "ValueObject",
    "event_bus",
]
