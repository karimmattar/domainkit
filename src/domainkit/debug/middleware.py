"""``DebugToolbarMiddleware``: what each request cost the database, while DEBUG."""

import cProfile
import logging
import time
from pathlib import Path
from typing import Final
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit import clock
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.context import logged_path
from domainkit.debug.queries import QueryLog, record_queries
from domainkit.exceptions import ImproperlyConfigured

PROFILE_HEADER: Final = "x-debug-profile"
"""Send ``X-Debug-Profile: 1`` to profile a request (with ``PROFILE_DIR``)."""
logger = logging.getLogger(__name__)
_profiling: list[cProfile.Profile] = []
"""The profile running now: Python runs one profiler at a time."""


class DebugToolbarMiddleware:
    """Time the SQL of each request; warn about likely N+1 queries.

    Every response gets ``Server-Timing`` (``db`` and the time to the
    response), and with ``DEBUG_TOOLBAR.HEADERS`` the ``X-Debug-*``
    counts. A statement shape run ``REPEATED_QUERIES`` times or more in one
    request is logged as a likely N+1, without values. With
    ``PROFILE_DIR``, a request sent with ``X-Debug-Profile: 1`` leaves a
    cProfile file there, named in the response's ``X-Debug-Profile``.

    It does nothing unless ``DEBUG`` is on, and refuses to run in staging
    and production.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``; refuse deployed environments."""
        if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
            raise ImproperlyConfigured(
                "DebugToolbarMiddleware is for development, not "
                f"{settings.ENVIRONMENT}."
            )
        self.app = app
        self.enabled = settings.DEBUG
        self.config = settings.DEBUG_TOOLBAR

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Record the request's statements and report them."""
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        profile_path = self._profile_path(scope)
        profile = cProfile.Profile() if profile_path else None

        with record_queries() as log:

            async def send_with_timing(message: Message) -> None:
                if message["type"] == "http.response.start":
                    self._annotate(message, log, started, profile_path)
                await send(message)

            if profile is not None:
                _profiling.append(profile)
                profile.enable()
            try:
                await self.app(scope, receive, send_with_timing)
            finally:
                if profile is not None and profile_path is not None:
                    profile.disable()
                    _profiling.remove(profile)
                    profile.dump_stats(profile_path)
                self._warn(scope, log)

    def _profile_path(self, scope: Scope) -> Path | None:
        """Where this request's profile goes, if it asked for one.

        None while another request is profiled: one profiler runs at a time.
        """
        directory = self.config.PROFILE_DIR
        if directory is None or _profiling:
            return None
        if Headers(scope=scope).get(PROFILE_HEADER) != "1":
            return None
        return directory / f"{clock.now():%Y%m%dT%H%M%S}-{uuid4().hex[:8]}.prof"

    def _annotate(
        self,
        message: Message,
        log: QueryLog,
        started: float,
        profile_path: Path | None,
    ) -> None:
        """Add the timing headers to the response start."""
        headers = MutableHeaders(scope=message)
        database = log.seconds * 1000
        total = (time.perf_counter() - started) * 1000
        headers.append(
            "Server-Timing",
            f'db;dur={database:.1f};desc="{log.count} queries", app;dur={total:.1f}',
        )
        if self.config.HEADERS:
            headers["X-Debug-Queries"] = str(log.count)
            headers["X-Debug-Query-Time"] = f"{database:.1f}"
            repeated = log.repeated(self.config.REPEATED_QUERIES)
            headers["X-Debug-Repeated-Queries"] = str(len(repeated))
        if profile_path is not None:
            headers["X-Debug-Profile"] = profile_path.name

    def _warn(self, scope: Scope, log: QueryLog) -> None:
        """Log each statement shape the request ran too often."""
        for shape, runs in log.repeated(self.config.REPEATED_QUERIES):
            logger.warning(
                "Possible N+1 in %s %s: %d runs of %s",
                scope["method"],
                logged_path(scope),
                runs,
                shape,
            )
