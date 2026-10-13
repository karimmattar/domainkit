"""Send unexpected errors to error trackers (Sentry, Rollbar, ...).

``ERROR_REPORTERS`` lists callables by dotted path; each receives the
exception and a context mapping (request id, path, message id, ...). A
reporter that fails is logged and never breaks the caller. For Sentry::

    def report(exc: BaseException, context: Mapping[str, object]) -> None:
        sentry_sdk.capture_exception(exc)
"""

import logging
from collections.abc import Callable, Mapping
from functools import cache

from domainkit.conf import settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.module_loading import import_string

type Reporter = Callable[[BaseException, Mapping[str, object]], None]

logger = logging.getLogger(__name__)


@cache
def _load(paths: tuple[str, ...]) -> tuple[Reporter, ...]:
    """Import the reporters named in ``ERROR_REPORTERS``."""
    reporters = []
    for path in paths:
        try:
            reporter = import_string(path)
        except ImportError as exc:
            raise ImproperlyConfigured(f"ERROR_REPORTERS: {exc}") from exc
        if not callable(reporter):
            raise ImproperlyConfigured(f"ERROR_REPORTERS: {path!r} is not callable.")
        reporters.append(reporter)
    return tuple(reporters)


def reporters() -> tuple[Reporter, ...]:
    """Return the configured reporters, failing fast on bad paths."""
    return _load(tuple(settings.ERROR_REPORTERS))


def report_error(exc: BaseException, **context: object) -> None:
    """Hand ``exc`` to every reporter; never raises."""
    for reporter in reporters():
        try:
            reporter(exc, context)
        except Exception:
            logger.exception("Error reporter %r failed", reporter)
