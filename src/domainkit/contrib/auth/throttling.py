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
behind one address, never uses its counts up with its own logins. Failures
do use them up, whoever sends them, and a full count refuses the next
attempt before its password is checked, the right one too: ``LOGIN_RATE``
wrong passwords for a username in a period, from any addresses, refuse
its user's right password with 429 until the count frees a place (one
every ``period / limit`` seconds), and ``LOGIN_RATE`` wrong passwords from
one address refuse every login from it, an office's or a carrier's
included. That is the price of the per-account count, which bounds
guessing one account from many addresses. A client that logged in to the
account before sends a device proof (``devices``), and is counted per
device instead: strangers' failures never reach its count. Checks of what cannot be
guessed (signed tokens) are counted after they fail (``count_failure``),
so a flood of forged tokens never holds a valid one back. An attempt the
counter cannot count (Redis is down) is refused with 503 rather than let
through, in staging and production, or wherever ``AUTH.THROTTLE_FAIL_CLOSED``
says so (``fails_closed``).
"""

import hashlib
import math
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from fastapi import Request, status

from domainkit import clock
from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.ddd.tenancy import shared_keys
from domainkit.http.errors import APIError
from domainkit.http.ratelimit import RATE_LIMITED, Backend, Decision, client_group
from domainkit.utils.module_loading import import_string
from domainkit.utils.rates import parse_rate

from .models import as_mobile_number
from .settings import auth_settings
from .users import get_user_model

AUDIT = "domainkit.contrib.audit"
MAX_REFUSALS = 10_000
"""Throttled keys remembered, to audit each once per period."""


@dataclass(frozen=True, slots=True)
class Window:
    """A rate held in fixed windows: at most its limit from each period's start.

    A plain rate is a bucket that refills smoothly (``http.ratelimit``): a
    period after an idle stretch can see about twice its limit. A window
    counts at most ``limit`` attempts in each period, aligned on the epoch
    (each UTC day for ``"100/day"``), as a daily bound must (SEC-16a-2): a
    burst of ``limit`` is let through, and nothing more until the next
    period starts.
    """

    rate: str
    """``"100/day"``: the limit, and the period of each window."""

    def bucket(self, now: float) -> tuple[str, int, float, int]:
        """The window ``now`` (epoch seconds) falls in, as a bucket that never refills.

        Its key's suffix, limit, and the bucket's period, past which one
        attempt would be refilled: ``limit + 1`` windows, so none is within
        this one; and the window's own period.
        """
        limit, period = parse_rate(self.rate)
        return f"@{int(now // period)}", limit, float(period * (limit + 1)), period


type Limit = tuple[str, str | Window | None]
"""A count's key, and its rate or window (None: not counted)."""


class Attempt:
    """What an attempt was counted against, to give back if it succeeds."""

    def __init__(self, counter: Backend | None) -> None:
        """Count in ``counter`` (None: nothing is counted)."""
        self.counter = counter
        self.counted: list[tuple[str, int, float]] = []
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
) -> Attempt:
    """Count an attempt against each of ``keys``; refuse it past ``rate``.

    Raises:
        APIError: 429 ``rate_limited``, with ``detail``, past the rate for a key;
            503 if it cannot be counted, failing closed (``fails_closed``).
    """
    limits = [(key, rate) for key in keys]
    return await attempt(request, limits, action=action, detail=detail)


async def attempt(
    request: Request,
    limits: Iterable[Limit],
    *,
    action: str,
    detail: str,
) -> Attempt:
    """Count an attempt against each ``(key, rate)``; refuse it past a rate.

    An attempt refused by one count is given back to those before it: it
    was not made. Call ``succeeded()`` on the result once the check holds.

    Raises:
        APIError: 429 ``rate_limited``, with ``detail``, past the rate for a key;
            503 ``throttle_unavailable`` if the counter could not count it,
            failing closed (``fails_closed``).
    """
    counted = [(key, rate) for key, rate in limits if rate is not None]
    taken = Attempt(counter(request) if counted else None)
    for name, rate in counted:
        key, limit, period, audited = _bucket(name, rate)
        decision = await counter(request).hit(key, limit, period)
        if decision.allowed and decision.counted:
            taken.counted.append((key, limit, period))
            continue
        if decision.allowed and not fails_closed():
            continue  # Let through uncounted, as other requests are.
        await taken.succeeded()  # Refused, so not made: give back what it took.
        if decision.allowed:
            raise _uncounted(decision)
        if first_refusal(request, key, audited):
            await audit_refusal(action, "throttled")
        raise _throttled(decision, detail)
    return taken


def _bucket(key: str, rate: str | Window) -> tuple[str, int, float, int]:
    """The key, limit, and period a count is hit with; its period for audits."""
    if isinstance(rate, Window):
        suffix, limit, period, window = rate.bucket(clock.now().timestamp())
        return key + suffix, limit, period, window
    limit, every = parse_rate(rate)
    return key, limit, every, every


def fails_closed() -> bool:
    """Whether an attempt the counter cannot count is refused (503), not let through.

    ``AUTH.THROTTLE_FAIL_CLOSED``; unset (None), True in staging and
    production and False elsewhere, since 0.11. A ``RedisBackend`` whose
    local fallback counted the attempt still says it could not
    (``Decision.counted``): the fallback counts in one process only.
    """
    explicit = auth_settings().THROTTLE_FAIL_CLOSED
    if explicit is None:
        return settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS
    return explicit


async def count_failure(
    request: Request,
    rate: str | None,
    key: str,
    *,
    action: str,
    detail: str,
) -> None:
    """Count a check that failed, after it was made; refuse past ``rate``.

    For what cannot be guessed, such as a signed token: valid ones are
    never counted, so forged ones, however many, never hold them back.
    The count bounds the work and the audit rows failures cost.

    Raises:
        APIError: 429 ``rate_limited``, with ``detail``, past the rate.
    """
    if rate is None:
        return
    limit, period = parse_rate(rate)
    decision = await counter(request).hit(key, limit, period)
    if not decision.allowed:
        if first_refusal(request, key, period):
            await audit_refusal(action, "throttled")
        raise _throttled(decision, detail)


def _throttled(decision: Decision, detail: str) -> APIError:
    """The 429 of a refused attempt: ``rate_limited``, as every 429 is (WEB-25)."""
    return APIError(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail,
        code=RATE_LIMITED,
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
    """The count of ``scope`` attempts from the request's client address.

    A client without an address (none, or the empty host Granian gives a
    unix socket's peer) counts as ``unknown``.
    """
    host = request.client.host if request.client else None
    return f"{scope}:ip:{client_group(host) if host else 'unknown'}"


def account_key(scope: str, username: str) -> str:
    """The count of ``scope`` attempts at the account ``username`` names.

    Names the lookup treats as one (``alice`` and its full-width or
    mathematical look-alikes, under NFKC) share one key, in any case.
    """
    return f"{scope}:user:{account_digest(username)}"


def account_digest(username: str) -> str:
    """The digest that names ``username``'s account in counts and proofs.

    Names the lookup treats as one share it (NFKC, any case), and so do
    the ways of writing one mobile number (``+33 6 12 34 56 78``).
    """
    name = get_user_model().normalize_username(username).strip()
    return _digest(as_mobile_number(name) or name)


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


_making_limiter: Final = threading.Lock()
"""Held to make an application's attempt counter: loops on threads ask at once."""


class _SharedCounter:
    """A counter whose keys are every tenant's: users and addresses are.

    Attempts are counted inside a tenant's request, but an account, a
    device, and an address belong to no tenant: per tenant, naming another
    one would start their counts afresh.
    """

    def __init__(self, counter: Backend) -> None:
        """Count in ``counter``."""
        self.counter = counter

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Count one attempt for ``key``, whatever the tenant."""
        with shared_keys():
            return await self.counter.hit(key, limit, period)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - refund, if any.
        """``refund``, if the counter can give attempts back."""
        if name != "refund":
            raise AttributeError(name)
        refund = self.counter.refund  # type: ignore[attr-defined]

        async def shared(key: str, limit: int, period: float) -> None:
            with shared_keys():
                await refund(key, limit, period)

        return shared


def limiter(request: Request) -> Backend:
    """The application's attempt counter (``RATE_LIMIT_BACKEND``), made once.

    Loops on two threads that ask at once get the same counter, so a memory
    counter sees every attempt. Count with ``counter``: its keys are every
    tenant's.
    """
    state = request.app.state
    counter: Backend | None = getattr(state, "login_limiter", None)
    if counter is None:
        with _making_limiter:
            counter = getattr(state, "login_limiter", None)
            if counter is None:
                counter = import_string(settings.RATE_LIMIT_BACKEND)()
                state.login_limiter = counter
    return counter


def counter(request: Request) -> Backend:
    """``limiter``, counting each key for every tenant at once (``_SharedCounter``)."""
    return _SharedCounter(limiter(request))


async def audit_refusal(action: str, reason: str) -> None:
    """Record a refused attempt, without what was tried: it may be a secret."""
    if registry.is_installed(AUDIT):
        from domainkit.contrib.audit import Outcome, record

        await record(action, Outcome.DENIED, reason=reason)


def _digest(text: str) -> str:
    """SHA-256 of ``text``, folded (NFKC, case), in hex."""
    folded = unicodedata.normalize("NFKC", text.casefold())
    return hashlib.sha256(folded.encode()).hexdigest()
