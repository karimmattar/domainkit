"""Settings of ``domainkit.contrib.devices``: ``DEVICES = {...}``."""

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["DevicesSettings"]


class DevicesSettings(BaseModel):
    """Passkeys and bound devices; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    RP_ID: str | None = None
    """The relying party passkeys are made for: your site's domain
    (``example.com``), which every origin must be within. None: no passkeys."""
    RP_NAME: str | None = None
    """The name a browser shows when a passkey is made; None: ``RP_ID``."""
    ORIGINS: tuple[str, ...] = ()
    """The pages that may make and use passkeys (``https://app.example.com``);
    an app's (``android:apk-key-hash:...``) too."""
    CHALLENGE_TTL: timedelta = Field(
        default=timedelta(minutes=5), gt=timedelta(0), le=timedelta(minutes=10)
    )
    """How long a challenge works: the browser's timeout too."""
    MAX_PASSKEYS: int = Field(default=10, ge=1, le=100)
    MAX_DEVICES: int = Field(default=10, ge=1, le=100)
    SIGNATURE_WINDOW: timedelta = Field(
        default=timedelta(minutes=2), gt=timedelta(0), le=timedelta(minutes=10)
    )
    """How far a signed request's time may be from the server's."""

    @model_validator(mode="after")
    def _origins_with_rp_id(self) -> "DevicesSettings":
        """Passkeys need the origins they come from."""
        if self.RP_ID is not None and not self.ORIGINS:
            raise ValueError("RP_ID needs ORIGINS: the pages passkeys are used on")
        return self
