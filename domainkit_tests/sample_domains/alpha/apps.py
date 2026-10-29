"""Configuration of the alpha sample domain."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_check, on_shutdown, on_startup


class AlphaSettings(BaseModel):
    """Values read from ``ALPHA``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    GREETING: str = "hello"


class AlphaConfig(DomainConfig):
    """Record every lifecycle call so tests can assert the order."""

    settings_class = AlphaSettings
    verbose_name = "Alpha sample"
    calls: ClassVar[list[str]] = []
    healthy: ClassVar[bool] = True

    def ready(self) -> None:
        """Record the call, and register the lifecycle hooks."""
        self.calls.append("ready")
        on_startup(self.start)
        on_shutdown(self.stop)
        on_check(self.probe)

    async def start(self) -> None:
        """Record the call."""
        self.calls.append("startup")

    async def stop(self) -> None:
        """Record the call."""
        self.calls.append("shutdown")

    async def probe(self) -> None:
        """Fail when the test marks the domain unhealthy."""
        if not self.healthy:
            raise RuntimeError("alpha is down")
