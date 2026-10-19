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
"""

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.conf import settings
from domainkit.i18n.translation import negotiate, override
from domainkit.utils.headers import patch_vary


class LocaleMiddleware:
    """Make the language ``Accept-Language`` asks for the request's language."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` with ``LANGUAGES`` and ``LANGUAGE_CODE``."""
        self.app = app
        self.languages = tuple(code for code, _ in settings.LANGUAGES)
        self.default = settings.LANGUAGE_CODE

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request in its language; label HTTP responses with it."""
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        header = Headers(scope=scope).get("accept-language")
        language = negotiate(header, self.languages, self.default)

        async def send_with_language(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                if "Content-Language" not in headers:
                    headers["Content-Language"] = language
                if len(self.languages) > 1:
                    patch_vary(headers, ["Accept-Language"])
            await send(message)

        with override(language):
            await self.app(scope, receive, send_with_language)
