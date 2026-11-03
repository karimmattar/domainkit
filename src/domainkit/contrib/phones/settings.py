"""Settings of ``domainkit.contrib.phones``: ``PHONES = {...}``."""

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["PhonesSettings"]


class PhonesSettings(BaseModel):
    """How codes are made, sent, and tried; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    CODE_LENGTH: int = Field(default=6, ge=6, le=10)
    """Digits in a code."""
    CODE_TTL: timedelta = Field(
        default=timedelta(minutes=10), gt=timedelta(0), le=timedelta(hours=1)
    )
    """How long a code works."""
    MAX_ATTEMPTS: int = Field(default=5, ge=1, le=10)
    """Wrong codes before the code is spent and a new one must be sent."""
    RESEND_AFTER: timedelta = Field(
        default=timedelta(minutes=1), ge=timedelta(0), le=timedelta(hours=1)
    )
    """How long a user waits before another code for the same number."""
    MAX_SENDS: int = Field(default=5, ge=1, le=100)
    """Codes sent in ``SEND_WINDOW`` to one number, and asked by one user:
    each a text message someone pays for."""
    SEND_WINDOW: timedelta = Field(
        default=timedelta(days=1), gt=timedelta(0), le=timedelta(days=30)
    )
    MAX_PHONES: int = Field(default=5, ge=1, le=100)
    """Verified numbers a user may hold."""
    MESSAGE: str = Field(default="Your verification code is {code}.", max_length=300)
    """The text sent; ``{code}`` is replaced by the code."""

    @field_validator("MESSAGE")
    @classmethod
    def _message_holds_the_code(cls, value: str) -> str:
        """A message with no ``{code}`` would send nothing to type."""
        if value.count("{code}") != 1 or value.replace("{code}", "").count("{") > 0:
            raise ValueError("MESSAGE holds {code} once, and no other {...}")
        return value
