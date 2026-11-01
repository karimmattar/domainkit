"""WEB-17: a money error in a batch answers the whole request 422 after items committed.

Without ``atomic``, each item of a ``batch_route`` is a use case of its
own, committed as it runs, and the answer (``207``) says what became of
each. ``domainkit.resources.batches.failure`` turns an item's refusal
into its result: a ``DomainError``, ``ServiceBusy``, an HTTP error, or a
database refusal. A ``MoneyError`` (``CurrencyMismatch``, an amount with
too many places, one out of range) is none of them, though the HTTP layer
answers it as a refusal (``422``, ``money_error_handler``): it leaves the
batch, and the client is told ``422 currency_mismatch`` for a request
whose first items were committed. A client that retries the refused
request runs those items again: a payment batch pays twice.
"""

from decimal import Decimal

import pytest

from domainkit.money import Money
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import InMemoryGadgets, make_gadget
from domainkit_tests.test_resources import (
    RenameGadget,
    RenameIn,
    as_,
    client_of,
    gadgets,
)
from domainkit_tests.test_resources import (
    rename as rename_gadget,
)


@pytest.fixture
def store() -> InMemoryGadgets:
    """Two gadgets."""
    return InMemoryGadgets([make_gadget("a"), make_gadget("b")])


async def rename_and_charge(command: RenameGadget, uow: object) -> object:
    """Rename; a name of ``eur`` then adds euros to dollars, as a mistake would."""
    renamed = await rename_gadget(command, uow)  # type: ignore[arg-type]
    if command.name == "eur":
        _ = Money.of(Decimal("1.00"), "USD") + Money.of(Decimal("1.00"), "EUR")
    return renamed


def test_an_item_refused_for_its_money_is_answered_with_the_others(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """The batch answers 207, the committed item 200, the refused one 422."""
    use_auth()
    resource = gadgets(store)
    resource.batch_route(RenameGadget, service=rename_and_charge, body=RenameIn)
    client = client_of(resource)
    first, second = list(store.items)

    answer = client.post(
        "/gadgets/batch",
        json={
            "items": [
                {"gadget_id": str(first), "name": "renamed"},
                {"gadget_id": str(second), "name": "eur"},
            ]
        },
        headers=as_("changer"),
    )

    assert store.items[first].name == "renamed"  # Committed, whatever is answered.
    assert answer.status_code == 207, answer.json()
    assert [each["status"] for each in answer.json()["results"]] == [200, 422]
