"""The content type entity: one row per installed model (``domainkit.db.models``)."""

from domainkit.ddd import Entity


class ContentType(Entity[int]):
    """A kind of stored object: its domain label and lower-case class name."""

    domain: str
    model: str

    def __str__(self) -> str:
        """``<domain>.<model>``, as in ``billing.invoice``."""
        return f"{self.domain}.{self.model}"
