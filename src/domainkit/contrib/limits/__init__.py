"""Limits on money movements: per transaction, per window, per subject, per currency.

Install it, then ``makemigrations`` and ``migrate``::

    INSTALLED_DOMAINS = ["domainkit.contrib.limits", ...]

    await set_limit(
        SetLimit(
            code="wallet.daily",
            kind="holder",
            currency="EGP",
            window=Window.DAY,
            max_amount=Money.of("50000", "EGP"),
        ),
        LimitsUnitOfWork(),
    )

A use case counts what it moves in its own unit of work (``consume``),
and a movement over a limit is refused (``LimitExceeded``, 422): two at
once never both pass a limit only one fits. With the ledger installed,
every entry it books is counted the same way, and a reversal gives back
what its entry counted. ``api.router`` serves the limits and their usage
over HTTP, by model permission. See ``docs/contrib/limits.md`` and ADR 0123.
"""

from .exceptions import (
    InvalidLimit,
    LimitExceeded,
    LimitNotFound,
    LimitTaken,
    TransactionLimitExceeded,
)
from .models import (
    ACCOUNT,
    ACTOR,
    HOLDER,
    Direction,
    Limit,
    LimitId,
    LimitRemoved,
    LimitSet,
    Movement,
    Window,
)
from .services import (
    ChangeLimit,
    RemoveLimit,
    SetLimit,
    change_limit,
    define_limit,
    remove_limit,
    set_limit,
)
from .settings import LimitDefault, LimitsSettings
from .uow import LimitsUnitOfWork
from .usage import Usage, check, consume, purge_usage, release, usage_of

__all__ = [
    "ACCOUNT",
    "ACTOR",
    "HOLDER",
    "ChangeLimit",
    "Direction",
    "InvalidLimit",
    "Limit",
    "LimitDefault",
    "LimitExceeded",
    "LimitId",
    "LimitNotFound",
    "LimitRemoved",
    "LimitSet",
    "LimitTaken",
    "LimitsSettings",
    "LimitsUnitOfWork",
    "Movement",
    "RemoveLimit",
    "SetLimit",
    "TransactionLimitExceeded",
    "Usage",
    "Window",
    "change_limit",
    "check",
    "consume",
    "define_limit",
    "purge_usage",
    "release",
    "remove_limit",
    "set_limit",
    "usage_of",
]
