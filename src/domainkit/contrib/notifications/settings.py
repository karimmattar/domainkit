"""Settings of ``domainkit.contrib.notifications``: ``NOTIFICATIONS = {...}``."""

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from domainkit.conf.global_settings import NAME_PATTERN

BACKENDS: Final = "domainkit.contrib.notifications.backends"


class NotificationsSettings(BaseModel):
    """Where text messages and push notifications go, and how often to retry."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    SMS_BACKEND: str = f"{BACKENDS}.ConsoleSmsBackend"
    """Dotted path to an ``SmsBackend``: a provider adapter of the project's."""
    PUSH_BACKEND: str = f"{BACKENDS}.ConsolePushBackend"
    """Dotted path to a ``PushBackend``: a provider adapter of the project's."""
    MAX_ATTEMPTS: int = Field(default=5, ge=1)
    """Tries of each message before it is dead-lettered."""
    QUEUE: str | None = Field(default=None, pattern=NAME_PATTERN)
    """The worker queue that sends them; None: ``default``."""
