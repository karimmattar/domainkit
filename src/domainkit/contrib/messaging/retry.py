"""How failed messages are retried: delays, and which errors are worth it.

``MESSAGING`` sets the defaults (``RETRY_BASE_SECONDS``,
``RETRY_MAX_SECONDS``); a task may set its own::

    @task(
        max_attempts=5,
        backoff=Backoff(base=1, cap=60),          # or Backoff.fixed(30)
        retry_on=(httpx.TransportError,),         # only these are retried
        dont_retry_on=(InvalidInvoice,),          # these dead-letter at once
    )
    async def push_invoice(invoice_id: UUID) -> None: ...
"""

import math
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Self

from domainkit.exceptions import ImproperlyConfigured

MAX_DELAY: Final = 365 * 24 * 3600.0
"""The longest retry delay, one year: longer ones would only overflow dates."""
_random = secrets.SystemRandom()

type Errors = tuple[type[Exception], ...]


@dataclass(frozen=True, slots=True)
class Backoff:
    """Seconds to wait before running a failed message again.

    The first retry waits ``base``; an exponential backoff doubles that
    after each attempt, up to ``cap``. With ``jitter``, each delay is drawn
    between half and all of it, so failures that happened together are
    retried apart.
    """

    base: float
    cap: float = 3600.0
    exponential: bool = True
    jitter: bool = True

    def __post_init__(self) -> None:
        """Refuse delays that are not positive, finite, and at most a year."""
        for name, value in (("base", self.base), ("cap", self.cap)):
            if not (math.isfinite(value) and 0 < value <= MAX_DELAY):
                raise ImproperlyConfigured(
                    f"Backoff {name} must be more than 0 and at most {MAX_DELAY:g} "
                    f"seconds, not {value!r}."
                )

    @classmethod
    def fixed(cls, seconds: float, *, jitter: bool = False) -> Self:
        """The same delay after every attempt."""
        return cls(seconds, seconds, exponential=False, jitter=jitter)

    def delay(self, attempts: int) -> float:
        """Seconds to wait after ``attempts`` runs (1 after the first)."""
        ceiling = self.base
        if self.exponential:
            # Past 2**1023 the float overflows; the cap has long been reached.
            ceiling *= 2.0 ** min(max(attempts - 1, 0), 1023)
        ceiling = min(ceiling, self.cap)
        return ceiling * _random.uniform(0.5, 1.0) if self.jitter else ceiling


def exception_types(errors: Iterable[object], option: str) -> Errors:
    """Check that ``errors`` are exception classes; return them as a tuple."""
    checked = tuple(errors)
    for error in checked:
        if not (isinstance(error, type) and issubclass(error, Exception)):
            raise ImproperlyConfigured(
                f"{option} takes exception classes, not {error!r}."
            )
    return checked  # type: ignore[return-value] # Checked just above.


def retryable(
    exc: BaseException, retry_on: Errors | None, dont_retry_on: Errors
) -> bool:
    """Whether a run that raised ``exc`` should be tried again.

    ``dont_retry_on`` wins; otherwise any error is retried, or, with
    ``retry_on``, only those of its types.
    """
    if isinstance(exc, dont_retry_on):
        return False
    return retry_on is None or isinstance(exc, retry_on)
