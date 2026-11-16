"""A resource that takes a file as an act, authorized by the real core (U3).

The shop's products take a leaflet, a PDF: ``PUT /products/{id}/leaflet``
asks ``StepUp(for_="shop.attach_leaflet")``, its twin asks for an
authorization of the file, and the service, on auth's database, spends
it with its commit. The ``Action`` declares nothing about the upload: it
binds the file by its content, not by the name the storage gives it.
"""

from uuid import UUID

from fastapi import APIRouter

from domainkit.contrib.auth import StepUp
from domainkit.contrib.auth.actions import Action, Shown, requires_authorization
from domainkit.contrib.auth.actions import router as actions_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.mfa import router as mfa_router
from domainkit.ddd import Command
from domainkit.http.uploads import CHECKED_PDF
from domainkit.resources import ModelActions, ResourceRouter
from domainkit.storage import MemoryStorage, StoredUpload
from domainkit_tests.sample_action_resources import ShopUnitOfWork
from domainkit_tests.sample_domains.shop.models import Product, ProductId

__all__ = ["ATTACH_LEAFLET", "LEAFLETS", "router"]

LEAFLETS = MemoryStorage()
"""Where the leaflets go; a test empties it."""


class AttachLeaflet(Command):
    """Give a product its leaflet."""

    product_id: UUID
    leaflet: StoredUpload


ATTACH_LEAFLET = Action(
    "shop.attach_leaflet",
    command=AttachLeaflet,
    subject=lambda command: f"shop.product:{command.product_id}",
    show=lambda command: Shown("Attach a leaflet", {"File": command.leaflet.filename}),
)


@requires_authorization(ATTACH_LEAFLET)
async def attach_leaflet(command: AttachLeaflet, uow: ShopUnitOfWork) -> Product:
    """Name the product after its leaflet; the commit spends the authorization."""
    async with uow:
        product = await uow.products.get(ProductId(command.product_id))
        product.name = command.leaflet.filename
        await uow.commit()
    return product


products = ResourceRouter(
    "/products",
    model=Product,
    unit_of_work=ShopUnitOfWork,
    repository=lambda uow: uow.products,
    rule=ModelActions(Product),
    lookup="product_id",
)
products.upload_route(
    AttachLeaflet,
    service=attach_leaflet,
    field="leaflet",
    types=[CHECKED_PDF],
    max_size=10_000,
    storage=LEAFLETS,
    method="PUT",
    path="/leaflet",
    rule=StepUp(for_="shop.attach_leaflet"),
)

router = APIRouter()
router.include_router(auth_router, prefix="/auth")
router.include_router(mfa_router, prefix="/auth")
router.include_router(actions_router, prefix="/auth")
router.include_router(products)
