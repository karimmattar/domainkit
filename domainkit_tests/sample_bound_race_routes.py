"""Acts bound to a product that someone renames while the act runs (ADR 0174).

Each service opens its unit of work its own way while another request
renames the product in a connection of its own: again on a retry
(``uow.run(..., retry=Retry(retry_on=(ConcurrentUpdate,)))``), in a
second transaction after a first that read, in one transaction that a
version check catches, or, ``binds`` locking the row, in one that does
not write the product; in a block opened inside another, after an inner
block's failure was caught, or in a second task's block. The act was
confirmed for the product as it was; it must not decide on the product
renamed. Once it decided and committed, a block that follows reads what
it saved, and passes.
"""

import asyncio
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import bindparam, text, update

from domainkit.contrib.auth import AuthenticatedUser, StepUp
from domainkit.contrib.auth.actions import (
    Action,
    ActionAuthorizationOut,
    Shown,
    request_action_authorization,
    requires_authorization,
)
from domainkit.ddd import Command, ConcurrentUpdate, DuplicateValue, Retry
from domainkit.http.access import Require
from domainkit_tests import sample_action_resources as shop
from domainkit_tests.sample_domains.shop.models import Product, ProductId
from domainkit_tests.sample_domains.shop.tables import products

__all__ = [
    "APPROVE",
    "APPROVED",
    "APPROVE_FREELY",
    "EVENTS",
    "LOCKING",
    "RENAMING",
    "router",
]

APPROVED: list[tuple[UUID, str]] = []
"""Each product approved, with the name the act decided on."""
EVENTS: list[str] = []
"""What happened, in order: reads, renames, a rename the lock held back."""
LOCKING: list[bool] = [False]
"""Whether ``binds`` reads the product for update, as the docs say to."""
RENAMING: list[bool] = [True]
"""Whether someone renames the product between a failed block and the next."""


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
APPROVE_FREELY = Action(
    "shop.approve_freely",
    command=Approve,
    subject=lambda command: f"shop.product:{command.product_id}",
    show=show,
)
"""The same act, binding nothing: its service opens its unit of work as it likes."""


async def rename(product_id: UUID, name: str) -> None:
    """Someone else renames the product, in a unit of work and connection of theirs.

    A rename a lock holds off for 1.5 seconds gives up, and says so.
    """
    try:
        await asyncio.wait_for(_rename(product_id, name), 1.5)
    except TimeoutError:
        EVENTS.append("the rename waited for the lock")


async def _rename(product_id: UUID, name: str) -> None:
    """Rename the product, and say so."""
    async with shop.ShopUnitOfWork() as other:
        stored = await other.products.get(ProductId(product_id))
        stored.name = name
        await other.commit()
    EVENTS.append(f"renamed to {name}")


async def decide(command: Approve, uow: Any) -> None:  # noqa: ANN401 - a uow.
    """Approve the product as ``uow`` reads it now: write it, commit."""
    product = await uow.products.get(ProductId(command.product_id))
    decided = product.name
    product.name = f"{decided} (approved)"
    await uow.commit()
    APPROVED.append((command.product_id, decided))


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
        await rename(command.product_id, "Lava lamp")
        product = await uow.products.get(ProductId(command.product_id))
        await uow.commit()
    APPROVED.append((command.product_id, product.name))


@requires_authorization(APPROVE)
async def approve_nested(command: Approve, uow: Any) -> None:  # noqa: ANN401 - a uow.
    """Read, then decide in a block a helper opens inside the first."""
    async with uow:
        await uow.products.get(ProductId(command.product_id))
        await rename(command.product_id, "Lava lamp")
        async with uow:
            await decide(command, uow)


@requires_authorization(APPROVE)
async def approve_after_a_caught_failure(command: Approve, uow: Any) -> None:  # noqa: ANN401
    """A block fails on the database (a duplicate key), and is caught; another decides.

    ``RENAMING`` says whether someone renames the product between the two.
    """
    twin = text(
        "INSERT INTO shop_products (id, name, version) VALUES (:id, 'Twin', 1)"
    ).bindparams(bindparam("id", type_=products.c.id.type))
    try:
        async with uow:
            await uow.session.execute(twin, {"id": command.product_id})
    except DuplicateValue:
        EVENTS.append("the first block failed")
    if RENAMING[0]:
        await rename(command.product_id, "Lava lamp")
    async with uow:
        await decide(command, uow)


@requires_authorization(APPROVE)
async def approve_gathered(command: Approve, uow: Any) -> None:  # noqa: ANN401 - a uow.
    """Two tasks read and write in the unit of work at once."""

    async def one(tag: str) -> None:
        async with uow:
            await asyncio.sleep(0)
            product = await uow.products.get(ProductId(command.product_id))
            await asyncio.sleep(0)
            await uow.products.add(
                Product(id=ProductId(uuid4()), name=f"{tag} {product.name}")
            )
            await uow.commit()
            EVENTS.append(f"task {tag} committed")

    results = await asyncio.gather(one("a"), one("b"), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


@requires_authorization(APPROVE)
async def approve_then_read(command: Approve, uow: Any) -> None:  # noqa: ANN401 - a uow.
    """Decide and commit, then read what was saved in a block of its own."""
    async with uow:
        await decide(command, uow)
    async with uow:
        product = await uow.products.get(ProductId(command.product_id))
    EVENTS.append(f"read {product.name}")


@requires_authorization(APPROVE)
async def approve_after_a_read_commit(command: Approve, uow: Any) -> None:  # noqa: ANN401
    """Read and commit, which writes nothing; then decide in a second block."""
    async with uow:
        await uow.products.get(ProductId(command.product_id))
        await uow.commit()
    await rename(command.product_id, "Lava lamp")
    async with uow:
        await decide(command, uow)


@requires_authorization(APPROVE)
async def approve_committing_twice(command: Approve, uow: Any) -> None:  # noqa: ANN401
    """Commit nothing, then decide and commit again in the same block."""
    async with uow:
        await uow.commit()
        await rename(command.product_id, "Lava lamp")
        await decide(command, uow)


@requires_authorization(APPROVE)
async def approve_in_sql(command: Approve, uow: Any) -> None:  # noqa: ANN401 - a uow.
    """Decide in SQL text, and in a Core statement on the session's connection."""
    async with uow:
        product = await uow.products.get(ProductId(command.product_id))
        bump = text("UPDATE shop_products SET version = version + 1 WHERE id = :id")
        await uow.session.execute(
            bump.bindparams(bindparam("id", type_=products.c.id.type)),
            {"id": product.id},
        )
        connection = await uow.session.connection()
        await connection.execute(
            update(products)
            .where(products.c.id == product.id)
            .values(name=f"{product.name} (approved)")
        )
        await uow.commit()
    APPROVED.append((command.product_id, product.name))


@requires_authorization(APPROVE_FREELY)
async def approve_freely(command: Approve, uow: Any) -> None:  # noqa: ANN401 - a uow.
    """What an act that binds may not do: as before 2.7, an act without ``binds`` may.

    A commit that reads, a decision after it, a block that follows.
    """
    async with uow:
        await uow.products.get(ProductId(command.product_id))
        await uow.commit()
    async with uow:
        await decide(command, uow)
    async with uow:
        product = await uow.products.get(ProductId(command.product_id))
    EVENTS.append(f"read {product.name}")


SERVICES: dict[str, Any] = {
    "retrying": approve_retrying,
    "in-two": approve_in_two,
    "written": approve_written,
    "unwritten": approve_unwritten,
    "nested": approve_nested,
    "caught": approve_after_a_caught_failure,
    "gathered": approve_gathered,
    "then-read": approve_then_read,
    "read-commit": approve_after_a_read_commit,
    "twice": approve_committing_twice,
    "in-sql": approve_in_sql,
    "freely": approve_freely,
}
ASKING = [Depends(Require(StepUp(for_=APPROVE)))]
ASKING_FREELY = [Depends(Require(StepUp(for_=APPROVE_FREELY)))]

router = APIRouter()
router.include_router(shop.router)


@router.post("/approve/{way}/{product_id}", dependencies=ASKING)
async def approve(way: str, product_id: UUID, user: AuthenticatedUser) -> Response:
    """Approve the product the way ``way`` names."""
    await SERVICES[way](Approve(product_id=product_id), shop.ShopUnitOfWork())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/approve-freely/{product_id}", dependencies=ASKING_FREELY)
async def approve_without_binding(
    product_id: UUID, user: AuthenticatedUser
) -> Response:
    """Approve the product with the act that binds nothing."""
    await approve_freely(Approve(product_id=product_id), shop.ShopUnitOfWork())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/approve-freely-authorization/{product_id}", status_code=201)
async def ask_freely(
    product_id: UUID, request: Request, user: AuthenticatedUser
) -> ActionAuthorizationOut:
    """Ask for an authorization of the act that binds nothing."""
    asked = await request_action_authorization(
        request,
        APPROVE_FREELY,
        Approve(product_id=product_id),
        unit_of_work=shop.ShopUnitOfWork,
    )
    return ActionAuthorizationOut.of(asked)


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
