"""Domain-driven design building blocks ("seedwork").

This package depends only on the standard library and ``domainkit.utils``, so
a domain layer that imports nothing but ``domainkit.ddd`` stays free of
frameworks and I/O.
"""

from domainkit.ddd.events import DomainEvent, EventBus, EventHandler, event_bus
from domainkit.ddd.exceptions import (
    BusinessRuleViolation,
    ConcurrentUpdate,
    Conflict,
    DomainError,
    DuplicateValue,
    NotFound,
    PermissionDenied,
)
from domainkit.ddd.messages import Command, Query
from domainkit.ddd.model import AggregateRoot, Entity, ValueObject
from domainkit.ddd.pagination import Page
from domainkit.ddd.repository import InMemoryRepository, Repository
from domainkit.ddd.uow import DeferredTask, UnitOfWork

__all__ = [
    "AggregateRoot",
    "BusinessRuleViolation",
    "Command",
    "ConcurrentUpdate",
    "Conflict",
    "DeferredTask",
    "DomainError",
    "DomainEvent",
    "DuplicateValue",
    "Entity",
    "EventBus",
    "EventHandler",
    "InMemoryRepository",
    "NotFound",
    "Page",
    "PermissionDenied",
    "Query",
    "Repository",
    "UnitOfWork",
    "ValueObject",
    "event_bus",
]
