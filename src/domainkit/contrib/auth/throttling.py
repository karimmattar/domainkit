"""Counts of attempts at what can be guessed or flooded, and their refusals.

Logging in, one-time codes, reset and verification links, and requests
that send mail are counted per client address and per account (or email
address), in ``RATE_LIMIT_BACKEND``. Past the rate, they get 429 with
``Retry-After``. The first refusal of each count in a period goes to the
audit trail, not every one, so a flood writes no rows to it. Accounts and
addresses are counted by digest: the counts hold no personal data.

Checks of what can be guessed (passwords, codes) are counted before they
are made, and given back when they succeed (``Attempt.succeeded``), so
only failures use a count up: a user who logs in often, or an office
behind one address, is never refused for logging in. Checks of what
cannot be guessed (signed tokens) are counted after they fail
(``count_failure``), so a flood of forged tokens never holds a valid one
back. With ``AUTH.THROTTLE_FAIL_CLOSED``, an attempt the counter cannot
count (Redis is down) is refused with 503 rather than let through.
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
from domainkit.http.ratelimit import Backend, Decision, client_group
from domainkit.utils.module_loading import import_string
from domainkit.utils.rates import parse_rate

from .settings import auth_settings
from .users import get_user_model

AUDIT = "domainkit.contrib.audit"
MAX_REFUSALS = 10_000
"""Throttled keys remembered, to audit each once per period."""

type Limit = tuple[str, str | None]
"""A count's key, and its rate (None: not counted)."""


class Attempt:
    """What an attempt was counted against, to give back if it succeeds."""

    def __init__(self, counter: Backend | None) -> None:
        """Count in ``counter`` (None: nothing is counted)."""
        self.counter = counter
        self.counted: list[tuple[str, int, int]] = []
        """The ``(key, limit, period)`` counts the attempt took."""

    async def succeeded(self) -> None:
        """The check held: give the counts back, so only failures use them up.

        A counter that cannot give back (no ``refund``) keeps them.
        """
        refund = getattr(self.counter, "refund", None)
        counted, self.counted = self.counted, []
        if refund is not None:
            for key, limit, period in counted:
                await refund(key, limit, period)


async def throttle(
    request: Request,
    rate: str | None,
    keys: Iterable[str],
    *,
    action: str,
    detail: str,
    code: str,
) -> Attempt:
    """Count an attempt against each of ``keys``; refuse it past ``rate``.

    Raises:
        APIError: 429, with ``detail`` and ``code``, past the rate for a key;
            503 with ``AUTH.THROTTLE_FAIL_CLOSED`` if it cannot be counted.
    """
    limits = [(key, rate) for key in keys]
    return await attempt(request, limits, action=action, detail=detail, code=code)


async def attempt(
    request: Request,
    limits: Iterable[Limit],
    *,
    action: str,
    detail: str,
    code: str,
) -> Attempt:
    """Count an attempt against each ``(key, rate)``; refuse it past a rate.

    An attempt refused by one count is given back to those before it: it
    was not made. Call ``succeeded()`` on the result once the check holds.

    Raises:
        APIError: 429, with ``detail`` and ``code``, past the rate for a key;
            503 ``throttle_unavailable`` with ``AUTH.THROTTLE_FAIL_CLOSED``
            if the counter could not count it.
    """
    counted = [(key, rate) for key, rate in limits if rate is not None]
    taken = Attempt(limiter(request) if counted else None)
    for key, rate in counted:
        limit, period = parse_rate(rate)
        decision = await limiter(request).hit(key, limit, period)
        if decision.allowed and decision.counted:
            taken.counted.append((key, limit, period))
            continue
        if decision.allowed and not auth_settings().THROTTLE_FAIL_CLOSED:
            continue  # Let through uncounted, as other requests are.
        await taken.succeeded()  # Refused, so not made: give back what it took.
        if decision.allowed:
            raise _uncounted(decision)
        if first_refusal(request, key, period):
            await audit_refusal(action, "throttled")
        raise _throttled(decision, detail, code)
    return taken


async def count_failure(
    request: Request,
    rate: str | None,
    key: str,
    *,
    action: str,
    detail: str,
    code: str,
) -> None:
    """Count a check that failed, after it was made; refuse past ``rate``.

    For what cannot be guessed, such as a signed token: valid ones are
    never counted, so forged ones, however many, never hold them back.
    The count bounds the work and the audit rows failures cost.

    Raises:
        APIError: 429, with ``detail`` and ``code``, past the rate.
    """
    if rate is None:
        return
    limit, period = parse_rate(rate)
    decision = await limiter(request).hit(key, limit, period)
    if not decision.allowed:
        if first_refusal(request, key, period):
            await audit_refusal(action, "throttled")
        raise _throttled(decision, detail, code)


def _throttled(decision: Decision, detail: str, code: str) -> APIError:
    """The 429 of a refused attempt."""
    return APIError(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail,
        code=code,
        headers={"Retry-After": str(math.ceil(decision.retry_after))},
    )


def _uncounted(decision: Decision) -> APIError:
    """The 503 of an attempt the counter could not count, failing closed."""
    return APIError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "Attempts cannot be counted right now; try again later.",
        code="throttle_unavailable",
        headers={"Retry-After": str(max(1, math.ceil(decision.retry_after)))},
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
