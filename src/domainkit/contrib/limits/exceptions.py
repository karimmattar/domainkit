"""What limits refuse, each with its code and HTTP status.

Messages are fixed sentences, so the HTTP boundary translates them. A
breach names the limit it broke on the error (``limit``, ``window``), for
the code that caught it; the answer to a client carries the code alone.
"""

from domainkit.ddd import BusinessRuleViolation, DuplicateValue, NotFound


class LimitNotFound(NotFound):
    """No limit has that id."""

    code = "limit_not_found"


class InvalidLimit(BusinessRuleViolation):
    """A limit, or a movement checked against limits, that cannot be as written."""

    code = "invalid_limit"


class LimitTaken(DuplicateValue):
    """Another limit has the same code, kind, direction, currency, and subject."""

    code = "limit_taken"


class LimitExceeded(BusinessRuleViolation):
    """A movement over what a limit allows in its window: an amount, or a count.

    Run again once the window has moved on, or the limit is raised.
    """

    code = "limit_exceeded"

    def __init__(self, message: str, *, limit: str = "", window: str = "") -> None:
        """Store the message, and the code and window of the limit broken."""
        super().__init__(message)
        self.limit = limit
        self.window = window


class TransactionLimitExceeded(LimitExceeded):
    """A movement over the most one movement may be (``per_transaction``).

    Running it again is refused again: split it, or raise the limit.
    """

    code = "transaction_limit_exceeded"
