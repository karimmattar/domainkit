"""Settings shared by every environment: wallets kept in double-entry books.

The example adds three apps to what ``domainkit init`` wrote: idempotency
(a retried request runs once), accounting (the books), and the wallets
domain, which keeps each customer's wallet as an account in them.

The pages these files name, docs/<page>.md, are domainkit's:
``python manage.py docs docs/auth.md`` prints a page's link for the
release installed.
"""

from pathlib import Path

from domainkit.conf import Env
from domainkit.log import build_logging_config

env = Env()
BASE_DIR = Path(__file__).resolve().parents[3]

PROJECT_NAME = "ledgerAPI"
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    "domainkit.contrib.idempotency",  # A retried POST runs once.
    "domainkit.contrib.accounting",  # Double-entry books: accounts, entries, holds.
    "domains.wallets",
]
# Users and logins (docs/auth.md). People log in with an email address or a
# mobile number, and prove they read it with a code of 6 digits; the
# username is optional (docs/auth/logging-in.md). `manage.py startdomain
# accounts --user` writes a user model of your own and names it here; do it
# before the first migrate. Other settings files extend this: AUTH = {**AUTH, ...}.
AUTH: dict[str, object] = {
    "IDENTIFIERS": ("email", "mobile_number"),
}
ROOT_ROUTER = "config.urls.router"
# /docs, /redoc, and /openapi.json follow DEBUG: on locally, off when
# deployed; API_DOCS = True serves them anywhere (docs/reference/http.md).
# Every request is authenticated; routes say who may call them (docs/auth.md).
MIDDLEWARE = [
    "domainkit.http.middleware.ProxyHeadersMiddleware",
    "domainkit.http.locale.LocaleMiddleware",  # Answers in LANGUAGES (docs/i18n.md).
    "domainkit.http.middleware.SecurityHeadersMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.deadlines.DeadlineMiddleware",  # REQUEST_TIMEOUT (docs/runtime.md).
    "domainkit.http.shedding.ConcurrencyLimitMiddleware",  # MAX_CONCURRENT_REQUESTS.
    "domainkit.http.middleware.AllowedHostsMiddleware",
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
    "domainkit.http.ratelimit.RateLimitMiddleware",  # Off until RATE_LIMIT is set.
    # Caps, queues, and clocks for WebSockets; re-checks their tokens (docs/live.md).
    "domainkit.http.websockets.WebSocketLimitsMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
    # Last: a keyed request's whole body is read, bounded by the limit above
    # (docs/contrib/idempotency.md). The wallets' money routes refuse requests
    # without an Idempotency-Key.
    "domainkit.contrib.idempotency.middleware.IdempotencyMiddleware",
]
ASGI_APPLICATION = "config.asgi:application"
# Error messages in the caller's language; dates shown in TIME_ZONE, stored in UTC.
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("ar", "Arabic")]
TIME_ZONE = "UTC"

# Lines leave through a bounded queue and a thread, so writing them never
# holds the event loop (docs/runtime.md, "Logging").
LOGGING = build_logging_config(log_format="text", level=env.str("LOG_LEVEL", "INFO"))
