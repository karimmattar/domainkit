"""Settings of ``domainkit.contrib.mail``: ``MAIL = {...}``."""

from pathlib import Path
from typing import Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from domainkit.conf.global_settings import NAME_PATTERN

from .message import check_address

SMTP_BACKEND: Final = "domainkit.contrib.mail.backends.SmtpBackend"
DEFAULT_PORTS: Final = {"tls": 465, "starttls": 587, "none": 25}
"""The port of each ``SECURITY``, when ``PORT`` names none."""


class SmtpSettings(BaseModel):
    """``MAIL["SMTP"]``: the server ``SmtpBackend`` hands messages to."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    HOST: str = Field(default="localhost", min_length=1, max_length=253)
    PORT: int | None = Field(default=None, ge=1, le=65535)
    """None: 465 for ``tls``, 587 for ``starttls``, 25 for ``none``."""
    SECURITY: Literal["starttls", "tls", "none"] = "starttls"
    """``none`` (plain text) is refused unless ``DEBUG`` is on."""
    USER: str | None = None
    PASSWORD: SecretStr | None = None
    """With ``USER``; hidden in reprs."""
    TIMEOUT: float = Field(default=10.0, gt=0)
    """Seconds to connect, and for each exchange with the server."""
    TLS_CA: Path | None = None
    """CA bundle that signed the server certificate; None uses the system's."""

    @model_validator(mode="after")
    def check_credentials(self) -> Self:
        """Keep the user and the password together."""
        if (self.USER is None) != (self.PASSWORD is None):
            raise ValueError("USER and PASSWORD go together")
        return self

    @property
    def port(self) -> int:
        """``PORT``, or the usual port of ``SECURITY``."""
        return self.PORT or DEFAULT_PORTS[self.SECURITY]


class MailSettings(BaseModel):
    """Where mail goes, who sends it, and how often sending is retried."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    BACKEND: str = SMTP_BACKEND
    """Dotted path to the backend class (Django's ``EMAIL_BACKEND``)."""
    DEFAULT_FROM_EMAIL: str = "webmaster@localhost"
    """The sender of messages that name none."""
    SMTP: SmtpSettings = SmtpSettings()
    MAX_ATTEMPTS: int = Field(default=5, ge=1)
    """Tries of each message before it is dead-lettered."""
    QUEUE: str | None = Field(default=None, pattern=NAME_PATTERN)
    """The worker queue that sends mail; None: ``default``."""
    TEMPLATE_DIRS: tuple[Path, ...] = ()
    """Directories searched for templates before the installed domains."""

    @field_validator("DEFAULT_FROM_EMAIL")
    @classmethod
    def _check_sender(cls, value: str) -> str:
        """Refuse what is not one address."""
        return check_address(value)
