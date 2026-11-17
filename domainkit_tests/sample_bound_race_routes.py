"""Acts bound to a product that someone renames while the act runs (ADR 0174).

Each service opens its unit of work its own way while another request
renames the product in a connection of its own: again on a retry
(``uow.run(..., retry=Retry(retry_on=(ConcurrentUpdate,)))``), in a
second transaction after a first that read, in one transaction that a
version check catches, or, ``binds`` locking the row, in one that does
not write the product. The act was confirmed for the product as it was;
it must not decide on the product renamed.
"""

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from domainkit.contrib.auth import AuthenticatedUser, StepUp
from domainkit.contrib.auth.actions import (
    Action,
    ActionAuthorizationOut,
    Shown,
    request_action_authorization,
    requires_authorization,
)
from domainkit.ddd import Command, ConcurrentUpdate, Retry
from domainkit.http.access import Require
from domainkit_tests import sample_action_resources as shop
from domainkit_tests.sample_domains.shop.models import ProductId

__all__ = ["APPROVE", "APPROVED", "EVENTS", "LOCKING", "router"]

APPROVED: list[tuple[UUID, str]] = []
"""Each product approved, with the name the act decided on."""
EVENTS: list[str] = []
"""What happened, in order: reads, renames, a rename the lock held back."""
LOCKING: list[bool] = [False]
"""Whether ``binds`` reads the product for update, as the docs say to."""


class Approve(Command):
    """Approve a product as it is named."""

    product_id: UUID


async def bound(command: Approve, uow: Any) -> dict[str, Any]:  # noqa: ANN401 - a uow.
    """The product's name and version: locked when ``LOCKING`` says so."""
    products = uow.products
    if LOCKING[0]:
        product = await products.get_for_update(ProductId(command.product_id))
    else:
        product = await products.get(ProductId(command.product_id))
    return {"name": product.name, "version": product.version}


async def show(command: Approve, uow: Any) -> Shown:  # noqa: ANN401 - a uow.
    """The product's name."""
    product = await uow.products.get(ProductId(command.product_id))
    return Shown("Approve a product", {"Product": product.name})


APPROVE = Action(
    "shop.approve_product",
    command=Approve,
    subject=lambda command: f"shop.product:{command.product_id}",
    show=show,
    binds=bound,
)


async def rename(product_id: UUID, name: str) -> None:
    """Someone else renames the product, in a unit of work and connection of theirs."""
    async with shop.ShopUnitOfWork() as other:
        stored = await other.products.get(ProductId(product_id))
        stored.name = name
        await other.commit()
    EVENTS.append(f"renamed to {name}")


@requires_authorization(APPROVE)
async def approve_retrying(command: Approve, uow: Any) -> None:  # noqa: ANN401 - a uow.
    """Run again on a lost race, as ``uow.run`` does: the rename comes in the first."""
    runs = 0

    async def work() -> None:
        nonlocal runs
        runs += 1
        async with uow:
            product = await uow.products.get(ProductId(command.product_id))
            if runs == 1:
                await rename(command.product_id, "Lava lamp")
            decided = product.name
            product.name = f"{decided} (approved)"
            await uow.commit()
        APPROVED.append((command.product_id, decided))

    await uow.run(work, retry=Retry(attempts=3, retry_on=(ConcurrentUpdate,)))


@requires_authorization(APPROVE)
async def approve_in_two(command: Approve, uow: Any) -> None:  # noqa: ANN401 - a uow.
    """Read in one transaction, decide in another; the rename comes between."""
    async with uow:
        await uow.products.get(ProductId(command.product_id))
    await rename(command.product_id, "Lava lamp")
    async with uow:
        product = await uow.products.get(ProductId(command.product_id))
        decided = product.name
        product.name = f"{decided} (approved)"
        await uow.commit()
    APPROVED.append((command.product_id, decided))


@requires_authorization(APPROVE)
async def approve_written(command: Approve, uow: Any) -> None:  # noqa: ANN401 - a uow.
    """Decide on the product ``binds`` loaded, and write it: its version holds it."""
    async with uow:
        if not EVENTS:
            await rename(command.product_id, "Lava lamp")
        product = await uow.products.get(ProductId(command.product_id))
        decided = product.name
        product.name = f"{decided} (approved)"
        await uow.commit()
    APPROVED.append((command.product_id, decided))


@requires_authorization(APPROVE)
async def approve_unwritten(command: Approve, uow: Any) -> None:  # noqa: ANN401 - a uow.
    """Decide without writing the product; a rename tries to come in meanwhile."""
    async with uow:
        try:
            await asyncio.wait_for(rename(command.product_id, "Lava lamp"), 1.5)
        except TimeoutError:
            EVENTS.append("the rename waited for the lock")
        product = await uow.products.get(ProductId(command.product_id))
        await uow.commit()
    APPROVED.append((command.product_id, product.name))


SERVICES: dict[str, Any] = {
    "retrying": approve_retrying,
    "in-two": approve_in_two,
    "written": approve_written,
    "unwritten": approve_unwritten,
}
ASKING = [Depends(Require(StepUp(for_=APPROVE)))]

router = APIRouter()
router.include_router(shop.router)


@router.post("/approve/{way}/{product_id}", dependencies=ASKING)
async def approve(way: str, product_id: UUID, user: AuthenticatedUser) -> Response:
    """Approve the product the way ``way`` names."""
    await SERVICES[way](Approve(product_id=product_id), shop.ShopUnitOfWork())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/approve-authorization/{product_id}", status_code=201)
async def ask(
    product_id: UUID, request: Request, user: AuthenticatedUser
) -> ActionAuthorizationOut:
    """Ask for an authorization of approving the product, read where the act reads."""
    asked = await request_action_authorization(
        request,
        APPROVE,
        Approve(product_id=product_id),
        unit_of_work=shop.ShopUnitOfWork,
    )
    return ActionAuthorizationOut.of(asked)
