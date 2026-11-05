"""Settings of ``domainkit.contrib.ciba``: ``CIBA = {...}``."""

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domainkit.utils.rates import parse_rate

__all__ = ["CibaSettings"]


class CibaSettings(BaseModel):
    """How sign-in requests reach the phone, last, and are limited; all optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    APPROVE_LOGINS: bool = False
    """Make the mobile app the user's identity: a login of a user with a
    bound device works only from that device, or once approved from it."""
    REQUEST_TTL: timedelta = Field(
        default=timedelta(minutes=2), gt=timedelta(0), le=timedelta(minutes=10)
    )
    """How long a push request waits for the phone (``expires_in``)."""
    QR_TTL: timedelta = Field(
        default=timedelta(seconds=60), gt=timedelta(0), le=timedelta(minutes=5)
    )
    """How long a QR code can be claimed, and its request waits for the phone."""
    INTERVAL: int = Field(default=5, ge=1, le=60)
    """Seconds an initiator waits between polls; sooner gets ``slow_down``."""
    MAX_PENDING: int = Field(default=3, ge=1, le=20)
    """Live push requests a user may have; past it, no push is sent."""
    REPORT_COOLDOWN: timedelta = Field(
        default=timedelta(minutes=15), ge=timedelta(0), le=timedelta(days=1)
    )
    """How long no push reaches a user who reported a request."""
    NUMBER_MATCHING: bool = True
    """Web push sign-ins show a two-digit code the phone must type."""
    REQUIRE_BINDING_MESSAGE: bool = True
    """CIBA clients must send a ``binding_message``, shown on both devices."""
    QR_SAME_NETWORK: Literal["warn", "require"] = "warn"
    """When the phone claiming a QR code is on another address than the page
    showing it: tell the app (``"warn"``), or refuse the claim. The
    project's own pages only: a CIBA client's server is not the page."""
    QR_FORMAT: str = Field(default="{code}", max_length=300)
    """What a QR code holds; ``{code}`` is replaced by the secret
    (``"https://id.example.com/qr#{code}"`` for a universal link)."""
    START_RATE: str | None = "10/minute"
    """Sign-ins the public routes start per client address."""
    CLIENT_RATE: str | None = "60/minute"
    """Requests each CIBA client starts."""
    ACCESS_TOKEN_TTL: timedelta = Field(
        default=timedelta(minutes=10), gt=timedelta(0), le=timedelta(hours=1)
    )
    """How long a CIBA client's access token works."""
    ID_TOKEN_TTL: timedelta = Field(
        default=timedelta(minutes=5), gt=timedelta(0), le=timedelta(hours=1)
    )
    """How long a CIBA client's ID token works."""
    ASSERTION_MAX_AGE: timedelta = Field(
        default=timedelta(minutes=5), gt=timedelta(0), le=timedelta(hours=1)
    )
    """The furthest a client's ``private_key_jwt`` assertion may expire."""
    PUSH_TITLE: str = Field(default="Approve sign-in?", min_length=1, max_length=100)
    PUSH_BODY: str = Field(
        default="Open the app to approve or deny it.", min_length=1, max_length=200
    )
    """The notification's text. No personal data: it goes through the push
    service; the app fetches the request's details itself."""

    @field_validator("QR_FORMAT")
    @classmethod
    def _format_holds_the_code(cls, value: str) -> str:
        """A QR code with no ``{code}`` would claim nothing."""
        if value.count("{code}") != 1 or value.replace("{code}", "").count("{") > 0:
            raise ValueError("QR_FORMAT holds {code} once, and no other {...}")
        return value

    @field_validator("START_RATE", "CLIENT_RATE")
    @classmethod
    def _check_rate(cls, value: str | None) -> str | None:
        """Refuse rates ``parse_rate`` cannot read."""
        if value is not None:
            parse_rate(value)
        return value
