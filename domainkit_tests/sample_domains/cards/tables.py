"""Tables of the cards domain, derived from the aggregates."""

from domainkit.db import map_imperatively, table_for

from .models import Cardholder, Payment

cardholders = table_for(Cardholder, "cards_cardholders", domain="cards")
payments = table_for(Payment, "cards_payments", domain="cards")


def start_mappers() -> None:
    """Map the aggregates to their tables."""
    map_imperatively(Cardholder, cardholders)
    map_imperatively(Payment, payments)
