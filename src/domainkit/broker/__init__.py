"""Message brokers: durable work queues for the worker (RabbitMQ, or in memory).

Configure them in ``BROKERS`` and point ``MESSAGING.BROKER`` at one; the
worker then takes its work from the broker instead of polling the outbox
table. The outbox stays the record of every message (see ADR 0014)::

    BROKERS = {"default": {"URL": "amqps://billing:pw@mq.internal/billing",
                           "PREFIX": "billing"}}
    MESSAGING = {"BROKER": "default"}
"""

from domainkit.broker.base import (
    Broker,
    BrokerError,
    BrokerRefused,
    BrokerUnavailable,
    Delivery,
    Handler,
    Subscription,
)
from domainkit.broker.handler import BrokerHandler, brokers
from domainkit.broker.memory import MemoryBroker

__all__ = [
    "Broker",
    "BrokerError",
    "BrokerHandler",
    "BrokerRefused",
    "BrokerUnavailable",
    "Delivery",
    "Handler",
    "MemoryBroker",
    "Subscription",
    "brokers",
]
