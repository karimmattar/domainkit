"""SEC-12a-6: ``If-Match`` is not held in the unit of work's later transactions.

``uow.expect`` checks the aggregate the first time a repository hands it
out, then never again ("each expectation is checked once"), so that the
use case's own changes do not trip it. The expectation outlives the
transaction that checked it: the route holds it on the unit of work for
the whole service, and a service may enter the unit of work again (a
plain command route hands it the unit of work itself; ``Joined`` opens
"a transaction of its own" for every block after the first). A service
that reads in one block, works outside a transaction (a payment, a
remote call), then writes in a second block, loads the aggregate afresh
there, at whatever version another client stored meanwhile, unchecked,
and commits over that client's change: the lost update ``If-Match``
exists to refuse. Optimistic locking guards only the second block.
Loaded in a transaction other than the one that checked it, the
aggregate must still be at a version the client read.
"""

from typing import Self
from uuid import uuid4

import pytest

from domainkit.ddd import InMemoryRepository
from domainkit.ddd.exceptions import PreconditionFailed
from domainkit_tests.test_resource_schemas import (
    Order,
    OrderId,
    OrdersUnitOfWork,
    make_order,
)


class Stored(OrdersUnitOfWork):
    """Each transaction loads the orders as stored, as a session does."""

    def __init__(self, rows: list[Order]) -> None:
        """Work on ``rows``, the store."""
        super().__init__(InMemoryRepository[Order, OrderId]([]))
        self.rows = rows

    async def __aenter__(self) -> Self:
        """A new transaction: what is stored now."""
        self.orders = InMemoryRepository[Order, OrderId](self.rows)
        return await super().__aenter__()


@pytest.mark.anyio
async def test_a_later_transaction_holds_to_the_version_the_client_read() -> None:
    """Read in one block, written in the next, after another client's change."""
    id_ = OrderId(uuid4())
    rows = [make_order(id=id_, version=3)]
    uow = Stored(rows)

    async def write() -> None:
        async with uow:  # The service writes, over version 4.
            await uow.orders.get(id_)
            await uow.commit()

    with uow.expect(Order, id_, {3}) as expected:  # If-Match: "3"
        async with uow:  # The service reads, and checks, then leaves.
            await uow.orders.get(id_)
        assert expected.checked
        rows[0] = make_order(id=id_, version=4)  # Another client's change.
        with pytest.raises(PreconditionFailed):
            await write()

    assert uow.commits == 0
