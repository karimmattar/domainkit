"""``GZipMiddleware``: compress responses for clients that accept gzip (Django's).

Listed in ``MIDDLEWARE`` (not by default), it compresses a response of
``GZIP_MINIMUM_SIZE`` bytes or more when the request's ``Accept-Encoding``
names gzip, at ``GZIP_LEVEL``, and adds ``Vary: Accept-Encoding``. A
response that is already encoded, and server-sent events, pass as they
are. Starlette's ``GZipMiddleware`` does the work.

Compressing a response that holds a secret and text the caller chose
lets an attacker who can watch its size guess the secret (BREACH): list
it for bodies that hold no secret, such as public lists and documents, or
keep tokens out of responses that reflect input.
"""

from starlette.middleware.gzip import GZipMiddleware as _StarletteGZip
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.conf import settings

__all__ = ["GZipMiddleware"]


class GZipMiddleware:
    """Compress responses at least ``GZIP_MINIMUM_SIZE`` bytes long."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` with Starlette's middleware, set from the settings."""
        self.app = _StarletteGZip(
            app,
            minimum_size=settings.GZIP_MINIMUM_SIZE,
            compresslevel=settings.GZIP_LEVEL,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request, its response compressed if it qualifies."""
        await self.app(scope, receive, send)
