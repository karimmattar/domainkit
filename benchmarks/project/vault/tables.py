"""The vault's tables, derived from the models."""

from domainkit.db import map_imperatively, table_for

from .models import Card, Wallet

wallets = table_for(Wallet, "vault_wallets", domain="vault")
cards = table_for(Card, "vault_cards", domain="vault")


def start_mappers() -> None:
    """Map the aggregates onto their tables (idempotent)."""
    map_imperatively(Wallet, wallets)
    map_imperatively(Card, cards)
