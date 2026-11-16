"""A resource whose command route asks ``StepUp(for_=...)`` (user report A6).

For the end-to-end test of action authorizations: the shop's products,
renamed one at a time. The route asks for an authorization of the rename,
the ``ResourceRouter`` mounts its twin (``POST
/products/{product_id}/rename/authorization``), and the service, on auth's
database, spends the authorization with its commit. Auth's routes log in,
enrol a second factor, and confirm an authorization with its code.

Relabelling is a ``@use_case`` (user report A13): ``/relabel`` runs the
copy ``requires_authorization`` makes, and asks; ``/relabel-directly``
runs the use case itself, as a route without an act (the phone's) does.
Its action takes the use case as its command, and shows the stored name,
read in the route's unit of work.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.contrib.auth import StepUp
from domainkit.contrib.auth.actions import Action, Shown, requires_authorization
from domainkit.contrib.auth.actions import router as actions_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.mfa import router as mfa_router
from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork
from domainkit.ddd import Command, use_case
from domainkit.resources import ModelActions, ResourceRouter
from domainkit_tests.sample_domains.shop.models import Product, ProductId

__all__ = ["RELABEL_PRODUCT", "RENAME_PRODUCT", "ShopUnitOfWork", "router"]


class Products(SqlAlchemyRepository[Product, ProductId]):
    """The shop's products."""

    model = Product


class ShopUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction on the products, in the default database: auth's."""

    products: Products

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.products = Products(session)


class RenameProduct(Command):
    """Give a product another name."""

    product_id: UUID
    name: str


async def show_rename(command: RenameProduct) -> Shown:
    """The product's name as stored, and the name it gets."""
    async with ShopUnitOfWork() as uow:
        product = await uow.products.get(ProductId(command.product_id))
    return Shown(
        "Rename a product", {"Product": product.name, "New name": command.name}
    )


RENAME_PRODUCT = Action(
    "shop.rename_product",
    command=RenameProduct,
    subject=lambda command: f"shop.product:{command.product_id}",
    show=show_rename,
)


@requires_authorization(RENAME_PRODUCT)
async def rename_product(command: RenameProduct, uow: ShopUnitOfWork) -> Product:
    """Rename the product; its commit spends the authorization."""
    async with uow:
        product = await uow.products.get(ProductId(command.product_id))
        product.name = command.name
        await uow.commit()
    return product


@use_case
async def relabel_product(uow: ShopUnitOfWork, product_id: UUID, name: str) -> Product:
    """Give a product another label: guarded only where a route asks."""
    product = await uow.products.get(ProductId(product_id))
    product.name = name
    return product


SHOWN_IN: list[ShopUnitOfWork] = []
"""The units of work ``show_relabel`` read in."""


async def show_relabel(command: Any, uow: ShopUnitOfWork) -> Shown:  # noqa: ANN401 - the use case's command.
    """The product's name, read in the unit of work it is given, and the new one."""
    SHOWN_IN.append(uow)
    product = await uow.products.get(ProductId(command.product_id))
    return Shown(
        "Relabel a product", {"Product": product.name, "New label": command.name}
    )


RELABEL_PRODUCT = Action(
    "shop.relabel_product",
    command=relabel_product,
    subject=lambda command: f"shop.product:{command.product_id}",
    show=show_relabel,
)


class NameIn(BaseModel):
    """The product's new name."""

    name: str


products = ResourceRouter(
    "/products",
    model=Product,
    unit_of_work=ShopUnitOfWork,
    repository=lambda uow: uow.products,
    rule=ModelActions(Product),
    lookup="product_id",
)
products.command_route(
    RenameProduct,
    service=rename_product,
    body=NameIn,
    path="/rename",
    rule=StepUp(for_="shop.rename_product"),
)

products.use_case(
    requires_authorization(RELABEL_PRODUCT)(relabel_product),
    path="/relabel",
    rule=StepUp(for_=RELABEL_PRODUCT),
)
products.use_case(relabel_product, path="/relabel-directly", name="relabel_directly")

router = APIRouter()
router.include_router(auth_router, prefix="/auth")
router.include_router(mfa_router, prefix="/auth")
router.include_router(actions_router, prefix="/auth")
router.include_router(products)
