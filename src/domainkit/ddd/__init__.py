"""Domain-driven design building blocks ("seedwork").

This package depends only on the standard library, ``domainkit.utils``,
``domainkit.clock``, and ``domainkit.deadlines``, so a domain layer that
imports nothing but ``domainkit.ddd`` stays free of frameworks and I/O.
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
    InvalidField,
    MultipleFound,
    NotFound,
    PermissionDenied,
    PreconditionFailed,
)
from domainkit.ddd.fields import (
    Digits,
    Encrypted,
    InvalidQuery,
    MaxLength,
    MinLength,
    Pattern,
    Range,
    Reference,
    Sensitive,
    UnknownField,
)
from domainkit.ddd.messages import UNSET, Command, Query, Unset
from domainkit.ddd.model import AggregateRoot, Entity, ValueObject
from domainkit.ddd.pagination import Page
from domainkit.ddd.personal import Counted, Personal, fields_about
from domainkit.ddd.query import (
    Criteria,
    F,
    Ordering,
    RowLock,
    Selection,
    Specification,
)
from domainkit.ddd.repository import (
    InMemoryRepository,
    LockNotAvailable,
    Repository,
    RowLocks,
    SoftDeletable,
)
from domainkit.ddd.stores import in_memory
from domainkit.ddd.uow import DeferredTask, ReadOnlyError, Retry, UnitOfWork
from domainkit.ddd.usecases import UseCase, use_case

__all__ = [
    "UNSET",
    "AggregateRoot",
    "BusinessRuleViolation",
    "Command",
    "ConcurrentUpdate",
    "Conflict",
    "Counted",
    "Criteria",
    "DeferredTask",
    "Digits",
    "DomainError",
    "DomainEvent",
    "DuplicateValue",
    "Encrypted",
    "Entity",
    "EventBus",
    "EventHandler",
    "F",
    "HandlerTimedOut",
    "InMemoryRepository",
    "InvalidField",
    "InvalidQuery",
    "LockNotAvailable",
    "MaxLength",
    "MinLength",
    "MultipleFound",
    "NotFound",
    "Ordering",
    "Page",
    "Pattern",
    "PermissionDenied",
    "Personal",
    "PreconditionFailed",
    "Query",
    "Range",
    "ReadOnlyError",
    "Reference",
    "Repository",
    "Retry",
    "RowLock",
    "RowLocks",
    "Selection",
    "Sensitive",
    "SoftDeletable",
    "Specification",
    "UnitOfWork",
    "UnknownField",
    "Unset",
    "UseCase",
    "ValueObject",
    "event_bus",
    "fields_about",
    "in_memory",
    "use_case",
]
