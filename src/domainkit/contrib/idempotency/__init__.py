"""Replay-safe unsafe requests with the ``Idempotency-Key`` header.

Install the app and add
``"domainkit.contrib.idempotency.middleware.IdempotencyMiddleware"`` to
``MIDDLEWARE`` after ``RequestBodyLimitMiddleware``. See ``middleware`` for
the rules.
"""

from .settings import IdempotencySettings

__all__ = ["IdempotencySettings"]
