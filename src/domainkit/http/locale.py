"""``LocaleMiddleware``: answer in the language the caller asks for.

Each request's ``Accept-Language`` picks a language of ``LANGUAGES``
(``LANGUAGE_CODE`` when none fits), which becomes the language of the
request: error details and validation messages are translated into it,
and ``gettext`` in endpoints uses it. Responses say which language they
are in (``Content-Language``) and, when more than one is possible, that
they vary with ``Accept-Language``, so shared caches keep one copy per
language.

Place it before ``RequestContextMiddleware``, so the errors that and every
later middleware answer with are translated too.

``LANGUAGES`` and ``LANGUAGE_CODE`` are read per request, so an
application answers as the settings say now, overridden in a test too.
"""

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.conf import settings
from domainkit.i18n.translation import negotiate, override
from domainkit.utils.headers import patch_vary

__all__ = [
    "LocaleMiddleware",
]


class LocaleMiddleware:
    """Make the language ``Accept-Language`` asks for the request's language."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``; ``LANGUAGES`` and ``LANGUAGE_CODE`` are read per request."""
        self.app = app
        self._codes: tuple[object, tuple[str, ...]] = (None, ())
        """The ``LANGUAGES`` last read, and their codes."""

    @property
    def languages(self) -> tuple[str, ...]:
        """The codes of ``LANGUAGES``, as the settings say now."""
        return tuple(code for code, _ in settings.LANGUAGES)

    @property
    def default(self) -> str:
        """``LANGUAGE_CODE``, as the settings say now."""
        return str(settings.LANGUAGE_CODE)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request in its language; label HTTP responses with it."""
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        # Kept settings are attributes of the proxy: two reads, no call. The
        # codes are worked out again only when LANGUAGES is another object.
        configured = settings.LANGUAGES
        read, languages = self._codes
        if read is not configured:
            languages = tuple(code for code, _ in configured)
            self._codes = (configured, languages)
        header = Headers(scope=scope).get("accept-language")
        language = negotiate(header, languages, settings.LANGUAGE_CODE)

        async def send_with_language(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                if "Content-Language" not in headers:
                    headers["Content-Language"] = language
                if len(languages) > 1:
                    patch_vary(headers, ["Accept-Language"])
            await send(message)

        with override(language):
            await self.app(scope, receive, send_with_language)
