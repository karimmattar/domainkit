"""A project that authenticates at a gateway, without ``domainkit.contrib.auth``.

The gateway in front of the service names the caller in ``X-User`` and the
permissions it holds in ``X-Permissions``. ``GatewayAuthentication`` puts a
``GatewayUser`` in ``scope["user"]``, or ``Nobody`` when the gateway named
no one; the gadgets resource is guarded by the core's rules alone, and
nothing here installs auth, its tables, or content types (ADR 0080).
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import Annotated, ClassVar, Final
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.ddd import Command, F
from domainkit.ddd.query import CriteriaLike
from domainkit.http.access import (
    AnonymousPrincipal,
    BasePermission,
    HasPermissions,
    NotAuthenticated,
    Public,
    current_user,
    reads_principal,
)
from domainkit.resources import FieldPolicy, ResourceRouter
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    Gadget,
    GadgetId,
    InMemoryGadgets,
)

STORE: Final = InMemoryGadgets()
"""The gadgets the project serves, in memory."""
VIEW: Final = "shop.view_gadget"
RENAME: Final = "shop.rename_gadget"
SEE_PRICE: Final = "shop.view_price"
CHALLENGE: Final = 'Gateway realm="shop"'


@dataclass(frozen=True, slots=True)
class GatewayUser:
    """Whom the gateway named, and the permissions it says they hold."""

    id: str
    permissions: frozenset[str]
    is_authenticated: ClassVar[bool] = True
    is_active: ClassVar[bool] = True

    async def ahas_perms(self, perms: Collection[str], obj: object = None) -> bool:
        """Whether the gateway granted every one of ``perms``."""
        return set(perms) <= self.permissions


@dataclass(frozen=True, slots=True)
class Nobody(AnonymousPrincipal):
    """A request the gateway named no one for: it says how to log in."""

    def challenge(self) -> str:
        """The gateway's ``WWW-Authenticate``."""
        return CHALLENGE


class GatewayAuthentication:
    """Put whom the gateway named in ``scope["user"]``."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Name the caller, then serve the request."""
        if scope["type"] in {"http", "websocket"}:
            headers = Headers(scope=scope)
            name = headers.get("x-user")
            scope["user"] = (
                GatewayUser(name, frozenset(headers.get("x-permissions", "").split()))
                if name
                else Nobody()
            )
        await self.app(scope, receive, send)


class OwnGadgets(BasePermission):
    """A caller renames only the gadgets named after them."""

    message = "Only the gadget's namesake may rename it."

    async def has_object_permission(
        self, request: HTTPConnection, user: GatewayUser, obj: object
    ) -> bool:
        """The gadget is named after the caller."""
        return isinstance(obj, Gadget) and obj.name.startswith(user.id)


class RenameGadget(Command):
    """Rename a gadget."""

    gadget_id: UUID
    name: str


class RenameIn(BaseModel):
    """A new name."""

    name: str = Field(max_length=30)


async def rename(command: RenameGadget, uow: FakeGadgetsUnitOfWork) -> Gadget:
    """Rename a gadget; the version moves on, as the database's would."""
    async with uow:
        gadget = await uow.gadgets.get(GadgetId(command.gadget_id))
        gadget.rename(command.name)
        gadget.version += 1
        await uow.commit()
    return gadget


async def active(request: HTTPConnection) -> CriteriaLike | None:
    """Callers reach the active gadgets only."""
    return F("active") == True  # noqa: E712 - a criterion, not a comparison.


gadgets = ResourceRouter(
    "/gadgets",
    model=Gadget,
    unit_of_work=lambda: FakeGadgetsUnitOfWork(STORE),
    repository=lambda uow: uow.gadgets,
    rule=HasPermissions(VIEW),
    policy=FieldPolicy(visible={"price": SEE_PRICE}),
    scope=active,
)
gadgets.command_route(
    RenameGadget,
    service=rename,
    body=RenameIn,
    path="/rename",
    rule=HasPermissions(RENAME),
)
gadgets.command_route(
    RenameGadget,
    service=rename,
    body=RenameIn,
    path="/rename-own",
    rule=OwnGadgets(),
    name="rename_own_gadget",
)


@reads_principal
async def gateway_user(request: HTTPConnection) -> GatewayUser:
    """The caller the gateway named; 401 with the gateway's challenge otherwise.

    Raises:
        NotAuthenticated: The gateway named no one.
    """
    user = current_user(request)
    if not isinstance(user, GatewayUser):
        raise NotAuthenticated(CHALLENGE)
    return user


router = APIRouter()
router.include_router(gadgets)


@router.get("/whoami")
async def whoami(user: Annotated[GatewayUser, Depends(gateway_user)]) -> dict[str, str]:
    """Whom the gateway named: the project's own dependency is the rule."""
    return {"user": user.id}


@router.get("/hello", dependencies=[Public])
async def hello() -> dict[str, str]:
    """Anyone, on purpose."""
    return {"hello": "world"}


forgotten = APIRouter()


@forgotten.get("/forgotten")
async def forgotten_route() -> dict[str, str]:
    """A route that declares no rule: deny by default refuses it."""
    return {}
