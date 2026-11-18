"""A clock for the deadlines of the upload checks, read without a stopwatch.

A check of ``domainkit.inspection`` reads ``time.monotonic()`` to set its
deadline, then now and then as it works, and refuses the file once a
reading is past the deadline. ``Stepped`` stands in for that module's
``time``: each reading is ``step`` seconds after the one before, however
fast the runner is. A deadline is then reached after a known number of
readings, and the readings say whether it was reached (TEST-01).
"""

from dataclasses import dataclass, field


@dataclass(slots=True)
class Stepped:
    """A clock that moves on ``step`` seconds each time it is read."""

    step: float
    readings: list[float] = field(default_factory=list)
    """Each reading, in order: the first sets the check's deadline."""

    def monotonic(self) -> float:
        """The next reading: 0 the first time, then ``step`` more each time."""
        self.readings.append(len(self.readings) * self.step)
        return self.readings[-1]

    def past(self, seconds: float) -> int:
        """How many readings were past a deadline ``seconds`` after the first."""
        deadline = self.readings[0] + seconds
        return sum(reading > deadline for reading in self.readings)
