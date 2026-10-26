"""Settings of ``domainkit.contrib.messaging``: ``MESSAGING = {...}``."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domainkit.conf import DEFAULT_DB_ALIAS

from .retry import MAX_DELAY


class MessagingSettings(BaseModel):
    """Delivery, retry, and worker options; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    DATABASE: str = DEFAULT_DB_ALIAS
    """Alias holding the outbox; units of work that defer tasks must use it."""
    MAX_ATTEMPTS: int = Field(default=10, ge=1)
    """Runs before a message is dead-lettered (``@task`` may override it)."""
    RETRY_BASE_SECONDS: float = Field(default=2.0, gt=0, le=MAX_DELAY)
    """First retry delay; it doubles per attempt, with jitter."""
    RETRY_MAX_SECONDS: float = Field(default=3600.0, gt=0, le=MAX_DELAY)
    TASK_TIMEOUT: float = Field(default=240.0, gt=0, le=24 * 3600)
    """Seconds one handler or task may run before it counts as failed."""
    VISIBILITY_TIMEOUT: float = Field(default=300.0, gt=0, le=2 * 24 * 3600)
    """Seconds a claimed message stays hidden; a crashed worker's work returns."""
    POLL_INTERVAL: float = Field(default=1.0, gt=0, le=3600)
    POLL_INTERVAL_IDLE: float | None = Field(default=None, gt=0, le=3600)
    """While commits wake the worker (``LISTEN``): the longest wait between polls.

    Each poll that finds nothing doubles the wait, from ``POLL_INTERVAL`` up
    to this; a claim or a wake-up starts again from ``POLL_INTERVAL``. It
    is also how late a message may start that becomes due without a
    commit waking anyone (a retry, a delay, an expired lock), and how late
    a periodic or cron run may start: the worker queues a periodic slot
    when it next polls, and a cron run is queued ahead, due later.
    ``None`` (the default): every wait is ``POLL_INTERVAL``, as before
    0.9.2.
    """
    BATCH_SIZE: int = Field(default=50, ge=1, le=10_000)
    CONCURRENCY: int = Field(default=10, ge=1, le=65_535)
    """Messages one worker process runs at once, from all its outboxes and queues.

    At most 65,535: RabbitMQ's prefetch, which it sets, is a 16-bit count.
    """
    SHUTDOWN_TIMEOUT: float = Field(default=20.0, ge=0, le=3600)
    """Seconds a stopping worker lets running messages finish; the rest run again."""
    RETENTION_SECONDS: float = Field(default=7 * 24 * 3600.0, gt=0, le=MAX_DELAY)
    """How long finished messages are kept; dead ones are kept until retried."""
    CLEANUP_INTERVAL: float = Field(default=3600.0, gt=0, le=24 * 3600)
    CLEANUP_BATCH_SIZE: int = Field(default=1000, ge=1, le=100_000)
    """Most finished messages one cleanup statement deletes."""
    BROKER: str | None = None
    """Alias in ``BROKERS``: workers take their work from it instead of polling."""
    REPUBLISH_AFTER: float = Field(default=3600.0, gt=0, le=7 * 24 * 3600)
    """With a broker: seconds a published message may wait before it goes again."""
    PUBLISH_ON_COMMIT: bool = True
    """With a broker: publish a commit's messages right after it, not on a poll."""
    BREAKER_FAILURES: int = Field(default=5, ge=1, le=10_000)
    """With a broker: failed publications in a row before commits stop publishing."""
    BREAKER_COOLDOWN: float = Field(default=30.0, gt=0, le=3600)
    """Seconds commits leave their messages to the relay once the breaker opens."""
    NOTIFY: bool = True
    """On PostgreSQL: commits wake idle workers at once (``LISTEN``/``NOTIFY``)."""
    METRICS_TTL: float = Field(default=15.0, ge=0, le=3600)
    """Seconds the outbox gauges are reused between scrapes (0: count every time)."""

    @model_validator(mode="after")
    def check_idle_polls(self) -> Self:
        """Never poll less often while listening than without a listener."""
        if (
            self.POLL_INTERVAL_IDLE is not None
            and self.POLL_INTERVAL_IDLE < self.POLL_INTERVAL
        ):
            raise ValueError("POLL_INTERVAL_IDLE must be at least POLL_INTERVAL")
        return self

    @model_validator(mode="after")
    def check_timeouts(self) -> Self:
        """Keep work hidden for longer than it may run."""
        if self.VISIBILITY_TIMEOUT <= self.TASK_TIMEOUT:
            raise ValueError("VISIBILITY_TIMEOUT must exceed TASK_TIMEOUT")
        return self
