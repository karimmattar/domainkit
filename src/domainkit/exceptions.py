"""Errors raised by the framework itself (``django.core.exceptions``).

Every exception domainkit defines is a ``DomainkitError``, so one
``except DomainkitError`` catches whatever the framework raised; each keeps
the standard base it had too (``ValueError``, ``TimeoutError``, starlette's
``HTTPException``), so the handlers written for those still catch it.
Business errors live in ``domainkit.ddd.exceptions``, under ``DomainError``.

Modules below ``domainkit.http`` never raise an HTTP exception: they raise
an error of their own, and ``domainkit.http.errors`` maps it to a response.
``ServiceBusy`` is such an error: the HTTP layer answers it with 503 and
``Retry-After``. Roots end in ``Error``; the others name what happened.
"""

from typing import ClassVar

__all__ = ["DomainkitError", "ImproperlyConfigured", "ServiceBusy"]


class DomainkitError(Exception):
    """The root of every exception domainkit raises."""


class ImproperlyConfigured(DomainkitError):
    """Settings, the environment, or the installed domains are invalid."""


class ServiceBusy(DomainkitError):
    """Something the operation needs is busy now; trying again later may work.

    The HTTP layer answers it with 503, its ``message`` and ``code``, and
    ``Retry-After: retry_after``; a command-line caller just sees the
    message. Subclasses set their own ``code``.
    """

    code: ClassVar[str] = "service_busy"

    def __init__(self, message: str, *, retry_after: int = 1) -> None:
        """Keep the message, and the seconds to wait before trying again."""
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after
