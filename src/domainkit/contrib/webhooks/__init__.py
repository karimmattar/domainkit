"""Outgoing webhooks: committed events, signed and delivered to subscribed URLs.

Install it with messaging, and the ``webhooks`` extra (httpx2, cryptography),
and set ``SECRET_KEY``: subscription secrets are stored encrypted::

    INSTALLED_DOMAINS = [
        "domainkit.contrib.messaging",
        "domainkit.contrib.webhooks",
        ...
    ]

    # In a DomainConfig.ready(): send these events to their subscribers.
    expose(InvoicePaid, "invoice.paid", payload=lambda e: {"invoice_id": e.invoice_id})

Staff manage subscriptions through the admin API (``webhooks.admin``);
``python manage.py webhooks`` lists deliveries and replays them. Deliveries
go through the outbox, so they happen only if the change commits, and are
signed as Standard Webhooks (``signing.verify`` checks one).
"""

from domainkit.deprecation import renamed

from .delivery import expose, queue_deliveries
from .guard import UnsafeTarget
from .settings import WebhooksSettings
from .signing import InvalidSignature, new_secret, sign, verify

__all__ = [
    "InvalidSignature",
    "UnsafeTarget",
    "WebhooksSettings",
    "expose",
    "new_secret",
    "queue_deliveries",
    "sign",
    "verify",
]

__getattr__ = renamed(
    __name__, {"WebhookSettings": f"{__name__}.WebhooksSettings"}, "2.0"
)
