"""``pytest --detect-blocking``: fail a test that holds its event loop.

asyncio's debug mode times every callback and task step, and logs the
slow ones (``Executing <Task ...> took 0.250 seconds``). With the option,
every event loop the tests start runs in debug mode (asyncio's and uvloop's
alike), and a test fails when a step of one of its loops took
``--blocking-seconds`` or longer: 0.1 by default, which is also as fine as
asyncio reports. Fixtures are not timed, only the test itself.

Debug mode also reports resources left open, as ``ResourceWarning``; with
``filterwarnings = error`` those fail tests too.
"""

import argparse
import logging
import os
from collections.abc import Iterator
from typing import Final

import pytest

DEFAULT_SECONDS: Final = 0.1
"""asyncio's own ``slow_callback_duration``: it reports nothing faster."""
DEBUG_VARIABLE: Final = "PYTHONASYNCIODEBUG"
"""Turns debug mode on for every loop asyncio or uvloop makes from then on."""


def seconds(value: str) -> float:
    """``--blocking-seconds``: a number of seconds, at least ``DEFAULT_SECONDS``.

    Raises:
        argparse.ArgumentTypeError: Not a number, or below the floor.
    """
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not number >= DEFAULT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"{value} is below {DEFAULT_SECONDS}, the least asyncio reports"
        )
    return number


class SlowSteps(logging.Handler):
    """Keeps asyncio's reports of steps that took ``seconds`` or longer."""

    def __init__(self, seconds: float) -> None:
        """Start with none seen."""
        super().__init__(logging.WARNING)
        self.seconds = seconds
        self.found: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the report if it is one, and slow enough."""
        if (
            isinstance(record.msg, str)
            and record.msg.startswith("Executing ")
            and isinstance(record.args, tuple)
            and isinstance(record.args[-1], float)
            and record.args[-1] >= self.seconds
        ):
            self.found.append(record.getMessage())


class BlockingDetector:
    """The plugin ``--detect-blocking`` registers for the session."""

    def __init__(self, seconds: float) -> None:
        """Turn debug mode on for the loops started from now on."""
        self.seconds = seconds
        self._previous = os.environ.get(DEBUG_VARIABLE)
        os.environ[DEBUG_VARIABLE] = "1"

    def pytest_unconfigure(self) -> None:
        """Put the environment back as it was."""
        if self._previous is None:
            os.environ.pop(DEBUG_VARIABLE, None)
        else:
            os.environ[DEBUG_VARIABLE] = self._previous

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self) -> Iterator[None]:
        """Fail the test if one of its loops reported a slow step."""
        steps = SlowSteps(self.seconds)
        asyncio_logger = logging.getLogger("asyncio")
        level = asyncio_logger.level
        if not asyncio_logger.isEnabledFor(logging.WARNING):
            asyncio_logger.setLevel(logging.WARNING)
        asyncio_logger.addHandler(steps)
        try:
            outcome = yield
        finally:
            asyncio_logger.removeHandler(steps)
            asyncio_logger.setLevel(level)
        if steps.found:
            listed = "\n".join(f"  {step}" for step in steps.found)
            pytest.fail(
                f"The test held its event loop for {self.seconds}s or more "
                f"(--detect-blocking):\n{listed}",
                pytrace=False,
            )
        return outcome


def add_options(group: pytest.OptionGroup) -> None:
    """Declare ``--detect-blocking`` and ``--blocking-seconds``."""
    group.addoption(
        "--detect-blocking",
        action="store_true",
        help="Fail tests that hold their event loop (asyncio's debug mode, "
        "for every loop the tests start).",
    )
    group.addoption(
        "--blocking-seconds",
        type=seconds,
        default=DEFAULT_SECONDS,
        metavar="SECONDS",
        help="How long one step may hold the loop under --detect-blocking "
        "(default and least: %(default)s).",
    )


def configure(config: pytest.Config) -> None:
    """Register the detector when ``--detect-blocking`` is given."""
    if config.getoption("detect_blocking"):
        detector = BlockingDetector(config.getoption("blocking_seconds"))
        config.pluginmanager.register(detector, "domainkit-detect-blocking")
