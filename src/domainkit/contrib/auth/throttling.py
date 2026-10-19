"""Counts of attempts at what can be guessed or flooded, and their refusals.

Logging in, one-time codes, reset and verification links, and requests
that send mail are counted per client address and per account (or email
address), in ``RATE_LIMIT_BACKEND``. Past the rate, they get 429 with
``Retry-After``. The first refusal of each count in a period goes to the
audit trail, not every one, so a flood writes no rows to it. Accounts and
addresses are counted by digest: the counts hold no personal data.
"""

import hashlib
import math
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable

from fastapi import Request, status

from domainkit import clock
from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.http.errors import APIError
from domainkit.http.ratelimit import Backend, client_group
from domainkit.utils.module_loading import import_string
from domainkit.utils.rates import parse_rate

from .users import get_user_model

AUDIT = "domainkit.contrib.audit"
MAX_REFUSALS = 10_000
"""Throttled keys remembered, to audit each once per period."""


async def throttle(
    request: Request,
    rate: str | None,
    keys: Iterable[str],
    *,
    action: str,
    detail: str,
    code: str,
) -> None:
    """Count an attempt against each of ``keys``; refuse it past ``rate``.

    Raises:
        APIError: 429, with ``detail`` and ``code``, past the rate for a key.
    """
    if rate is None:
        return
    limit, period = parse_rate(rate)
    counter = limiter(request)
    for key in keys:
        decision = await counter.hit(key, limit, period)
        if not decision.allowed:
            if first_refusal(request, key, period):
                await audit_refusal(action, "throttled")
            raise APIError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail,
                code=code,
                headers={"Retry-After": str(math.ceil(decision.retry_after))},
            )


def address_key(request: Request, scope: str) -> str:
    """The count of ``scope`` attempts from the request's client address."""
    client = client_group(request.client.host) if request.client else "unknown"
    return f"{scope}:ip:{client}"


def account_key(scope: str, username: str) -> str:
    """The count of ``scope`` attempts at the account ``username`` names.

    Names the lookup treats as one (``alice`` and its full-width or
    mathematical look-alikes, under NFKC) share one key, in any case.
    """
    name = get_user_model().normalize_username(username)
    return f"{scope}:user:{_digest(name)}"


def email_key(scope: str, email: str) -> str:
    """The count of ``scope`` attempts that send mail to ``email``."""
    return f"{scope}:email:{_digest(email.strip())}"


def first_refusal(request: Request, key: str, period: int) -> bool:
    """Whether ``key`` was refused for the first time in ``period`` here."""
    state = request.app.state
    refused: OrderedDict[str, float] | None = getattr(state, "login_refusals", None)
    if refused is None:
        refused = state.login_refusals = OrderedDict()
    now = clock.monotonic()
    since = refused.get(key)
    if since is not None and now - since < period:
        return False
    refused[key] = now
    refused.move_to_end(key)
    if len(refused) > MAX_REFUSALS:
        refused.popitem(last=False)
    return True


def limiter(request: Request) -> Backend:
    """The application's attempt counter (``RATE_LIMIT_BACKEND``), made once."""
    state = request.app.state
    counter: Backend | None = getattr(state, "login_limiter", None)
    if counter is None:
        counter = import_string(settings.RATE_LIMIT_BACKEND)()
        state.login_limiter = counter
    return counter


async def audit_refusal(action: str, reason: str) -> None:
    """Record a refused attempt, without what was tried: it may be a secret."""
    if registry.is_installed(AUDIT):
        from domainkit.contrib.audit import Outcome, record

        await record(action, Outcome.DENIED, reason=reason)


def _digest(text: str) -> str:
    """SHA-256 of ``text``, folded (NFKC, case), in hex."""
    folded = unicodedata.normalize("NFKC", text.casefold())
    return hashlib.sha256(folded.encode()).hexdigest()
