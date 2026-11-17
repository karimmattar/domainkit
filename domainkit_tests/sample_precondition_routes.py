"""Acts bound to the version their page read (ADR 0178, user report A25).

A reviewer reads a file (a shop product stands in for it) and its
``ETag``; the company replaces it under the same name, which raises its
version and leaves what the phone shows alike; the reviewer approves.
``/files/{id}/approve`` is a resource's act whose action asks for
``If-Match`` (``require_if_match=True``). ``/own/{id}/approve`` and its
twin are routes of the project's own: each holds its unit of work to the
request's ``If-Match`` (``uow.expect``), and the twin passes it to
``request_action_authorization``. ``/plain/{id}/approve/authorization``
asks for the same act with an action that does not ask for ``If-Match``,
as one asked before a deploy that added it. The shop's routes, auth's
included, come along.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status

from domainkit.contrib.auth import AuthenticatedUser, StepUp
from domainkit.contrib.auth.actions import (
    Action,
    ActionAuthorizationOut,
    Shown,
    request_action_authorization,
    requires_authorization,
)
from domainkit.ddd import Command, use_case
from domainkit.http.access import Require
from domainkit.resources import ModelActions, ResourceRouter, expected_versions
from domainkit_tests import sample_action_resources as shop
from domainkit_tests.sample_domains.shop.models import Product, ProductId

__all__ = [
    "APPROVED",
    "APPROVE_FILE",
    "APPROVE_OWN",
    "APPROVE_PLAIN",
    "RACING",
    "replace",
    "router",
]

APPROVED: list[tuple[UUID, int]] = []
"""Each file approved, with the version the act decided on."""
RACING: list[bool] = [False]
"""Whether the file is replaced as ``binds`` reads it at the twin."""


async def replace(product_id: UUID) -> None:
    """The company replaces the file under the same name: a new version, alike."""
    async with shop.ShopUnitOfWork() as uow:
        stored = await uow.products.get(ProductId(product_id))
        stored.name = f"{stored.name} "
        await uow.commit()
    async with shop.ShopUnitOfWork() as uow:
        stored = await uow.products.get(ProductId(product_id))
        stored.name = stored.name.rstrip()
        await uow.commit()


@use_case
async def approve_file(uow: shop.ShopUnitOfWork, product_id: UUID) -> Product:
    """Approve the file as stored."""
    product = await uow.products.get(ProductId(product_id))
    APPROVED.append((product.id, product.version))
    return product


async def bound_file(command: Any, uow: shop.ShopUnitOfWork) -> dict[str, Any]:  # noqa: ANN401 - a command.
    """What approving decides on: the file's version; replaced first when racing."""
    if RACING[0]:
        RACING[0] = False
        await replace(command.product_id)
    product = await uow.products.get(ProductId(command.product_id))
    return {"version": product.version}


async def show_file(command: Any, uow: shop.ShopUnitOfWork) -> Shown:  # noqa: ANN401 - a command.
    """The file's name: alike for the file and its replacement."""
    product = await uow.products.get(ProductId(command.product_id))
    return Shown("Approve a file", {"File": product.name})


APPROVE_FILE = Action(
    "shop.approve_file",
    command=approve_file,
    subject=lambda command: f"shop.product:{command.product_id}",
    show=show_file,
    binds=bound_file,
    require_if_match=True,
)


class ApproveOwn(Command):
    """Approve a file, on a route of the project's own."""

    product_id: UUID


APPROVE_OWN = Action(
    "shop.approve_own",
    command=ApproveOwn,
    subject=lambda command: f"shop.product:{command.product_id}",
    show=show_file,
    binds=bound_file,
)
APPROVE_PLAIN = Action(
    "shop.approve_own",
    command=ApproveOwn,
    subject=lambda command: f"shop.product:{command.product_id}",
    show=show_file,
    binds=bound_file,
)
"""The same act, asked by a twin that does not pass ``If-Match`` on."""
APPROVE_REQUIRING = Action(
    "shop.approve_own",
    command=ApproveOwn,
    subject=lambda command: f"shop.product:{command.product_id}",
    show=show_file,
    binds=bound_file,
    require_if_match=True,
)
"""The same act once its action asks for ``If-Match``."""


async def _approve(command: ApproveOwn, uow: shop.ShopUnitOfWork) -> None:
    """Approve the file as stored, in one block."""
    async with uow:
        product = await uow.products.get(ProductId(command.product_id))
        APPROVED.append((product.id, product.version))
        await uow.commit()


approve_own = requires_authorization(APPROVE_OWN)(_approve)
approve_requiring = requires_authorization(APPROVE_REQUIRING)(_approve)

files = ResourceRouter(
    "/files",
    model=Product,
    unit_of_work=shop.ShopUnitOfWork,
    repository=lambda uow: uow.products,
    rule=ModelActions(Product),
    lookup="product_id",
    basename="file",
)
files.use_case(
    requires_authorization(APPROVE_FILE)(approve_file),
    path="/approve",
    rule=StepUp(for_=APPROVE_FILE),
)

router = APIRouter()
router.include_router(shop.router)
router.include_router(files)
IfMatch = Annotated[str | None, Header()]


async def _ask(
    request: Request, action: Action, product_id: UUID, if_match: str | None
) -> ActionAuthorizationOut:
    """Ask for ``action`` on the file, held to ``if_match`` where binds reads it."""
    uow = shop.ShopUnitOfWork()
    versions = None if if_match is None else expected_versions(if_match)
    with uow.expect(Product, ProductId(product_id), versions):
        asked = await request_action_authorization(
            request,
            action,
            ApproveOwn(product_id=product_id),
            unit_of_work=lambda: uow,
            if_match=if_match,
        )
    return ActionAuthorizationOut.of(asked)


async def _act(service: Any, product_id: UUID, if_match: str | None) -> Response:  # noqa: ANN401 - a service.
    """Approve the file through ``service``, held to ``if_match``."""
    uow = shop.ShopUnitOfWork()
    versions = None if if_match is None else expected_versions(if_match)
    with uow.expect(Product, ProductId(product_id), versions):
        await service(ApproveOwn(product_id=product_id), uow)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/own/{product_id}/approve/authorization", status_code=201)
async def ask_own(
    product_id: UUID,
    request: Request,
    user: AuthenticatedUser,
    if_match: IfMatch = None,
) -> ActionAuthorizationOut:
    """The twin of a route of the project's own: ``If-Match`` passed on."""
    return await _ask(request, APPROVE_OWN, product_id, if_match)


@router.post("/plain/{product_id}/approve/authorization", status_code=201)
async def ask_plain(
    product_id: UUID, request: Request, user: AuthenticatedUser
) -> ActionAuthorizationOut:
    """The same act, asked with no ``If-Match``, as before 2.8."""
    return await _ask(request, APPROVE_PLAIN, product_id, None)


@router.post(
    "/own/{product_id}/approve",
    dependencies=[Depends(Require(StepUp(for_=APPROVE_OWN)))],
)
async def act_own(
    product_id: UUID, user: AuthenticatedUser, if_match: IfMatch = None
) -> Response:
    """The act on a route of the project's own, held to its ``If-Match``."""
    return await _act(approve_own, product_id, if_match)


@router.post(
    "/requiring/{product_id}/approve",
    dependencies=[Depends(Require(StepUp(for_=APPROVE_REQUIRING)))],
)
async def act_requiring(
    product_id: UUID, user: AuthenticatedUser, if_match: IfMatch = None
) -> Response:
    """The act, its action now asking for ``If-Match``."""
    return await _act(approve_requiring, product_id, if_match)
