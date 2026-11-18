"""How the payouts domain is stored: its table, and its unit of work in SQL.

``table_for`` makes the table from the aggregate's fields, so a field is
declared once, in domain.py; run ``makemigrations`` after changing one.
The unit of work's repositories come from ``PayoutsUnitOfWork``'s
annotations: a SQL repository for each. A port that declares a query of
its own needs a class that writes it here, annotated on the unit of work
(``payouts: SqlAlchemyPayoutRepository``). The domain's default
``DomainConfig`` calls ``start_mappers()`` at startup.
"""

from domainkit.db import SqlAlchemyUnitOfWork, map_imperatively, table_for

from domains.payouts.domain import Payout, PayoutsUnitOfWork

payouts = table_for(Payout, "payouts_payouts", domain="payouts")


class SqlAlchemyPayoutsUnitOfWork(SqlAlchemyUnitOfWork, PayoutsUnitOfWork):
    """One database transaction over the payouts repositories."""


def start_mappers() -> None:
    """Map the aggregates onto their tables (idempotent)."""
    map_imperatively(Payout, payouts)
