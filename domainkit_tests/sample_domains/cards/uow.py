"""The unit of work of the cards domain."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork

from .models import Cardholder, Payment


class Cardholders(SqlAlchemyRepository[Cardholder, UUID]):
    """Card holders."""

    model = Cardholder


class Payments(SqlAlchemyRepository[Payment, UUID]):
    """Payments."""

    model = Payment


class CardsUnitOfWork(SqlAlchemyUnitOfWork):
    """Card holders and payments."""

    def create_repositories(self, session: AsyncSession) -> None:
        """One repository per aggregate."""
        self.cardholders = Cardholders(session)
        self.payments = Payments(session)
