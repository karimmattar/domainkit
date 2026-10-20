"""How long a domain keeps its rows: ``Retention`` policies on ``DomainConfig``."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class Retention:
    """How long rows of one table, or one aggregate's personal data, are kept.

    Declare policies on the domain's configuration; ``manage.py privacy
    purge`` applies them::

        class ClinicConfig(DomainConfig):
            RETENTION = (
                Retention(
                    table="clinic_access_log",
                    field="accessed_at",
                    keep=timedelta(days=90),
                ),
                Retention(
                    aggregate=Visit,
                    field="visited_at",
                    keep=timedelta(days=3650),
                    erase=True,
                ),
            )

    A table's rows older than ``keep`` (by ``field``, a ``UTCDateTime`` or
    ``Date`` column) are deleted. With ``erase``, an aggregate's rows are
    kept and their ``Personal`` fields erased instead, through units of
    work, as ``privacy erase`` does; every such field must be optional.
    """

    field: str
    """The column (or the aggregate's field) that dates each row."""
    keep: timedelta
    table: str | None = None
    aggregate: type | None = None
    erase: bool = False

    def __post_init__(self) -> None:
        """Refuse policies that name both a table and an aggregate, or neither.

        Raises:
            ValueError: The policy cannot be applied.
        """
        if (self.table is None) == (self.aggregate is None):
            raise ValueError("A Retention names a table or an aggregate, not both.")
        if self.keep <= timedelta(0):
            raise ValueError("A Retention keeps rows for a positive time.")
        if self.erase and self.aggregate is None:
            raise ValueError("erase=True erases an aggregate's fields; name it.")
