"""The vault's repositories and unit of work."""

from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork

from .models import Card, CardId, Wallet, WalletId


class WalletRepository(SqlAlchemyRepository[Wallet, WalletId]):
    """Wallets by id."""

    model = Wallet


class CardRepository(SqlAlchemyRepository[Card, CardId]):
    """Cards by id."""

    model = Card


class VaultUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the wallets and cards."""

    wallets: WalletRepository
    cards: CardRepository

    def create_repositories(self, session: AsyncSession) -> None:
        """Open the repositories on ``session``."""
        self.wallets = WalletRepository(session)
        self.cards = CardRepository(session)
