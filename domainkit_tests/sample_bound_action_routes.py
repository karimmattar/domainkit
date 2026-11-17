"""An act bound to the record it decides on, kept elsewhere (ADR 0174, user report A24).

A page's text, kept in memory, not on auth's database: publishing it
binds the text the user was shown (``binds``). ``/pages/{id}/publish``
spends the authorization before the act, in a transaction of its own,
and reads the text again when the service opens its unit of work;
``/in-code`` spends it in code (``spend_authorization``), in a unit of
work it opened; ``/committed`` runs a service that commits without
opening its unit of work, which reads the text as it commits.
``/unopened`` runs a service that never opens its unit of work,
``/no-unit`` one given none: both are mistakes an act that binds refuses.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from domainkit.contrib.auth import AuthenticatedUser, StepUp
from domainkit.contrib.auth.actions import (
    Action,
    ActionAuthorizationOut,
    Shown,
    request_action_authorization,
    requires_authorization,
    spend_authorization,
    spent_in_code,
)
from domainkit.contrib.auth.actions import router as actions_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.mfa import router as mfa_router
from domainkit.ddd import Command, NotFound, UnitOfWork
from domainkit.http.access import Require

__all__ = ["PAGES", "PUBLISHED", "PUBLISH_PAGE", "router"]

PAGES: dict[UUID, str] = {}
"""Each page's text."""
PUBLISHED: list[tuple[UUID, str]] = []
"""Each page published, with the text it was published with."""


class PublishPage(Command):
    """Publish a page as it reads."""

    page_id: UUID


class PagesUnitOfWork(UnitOfWork):
    """Pages kept in memory: not auth's database."""

    def __init__(self) -> None:
        """Read the pages."""
        super().__init__()
        self.texts = PAGES

    async def _commit(self) -> None:
        """Nothing to write."""

    async def _rollback(self) -> None:
        """Nothing to undo."""


def bound_text(command: PublishPage, uow: PagesUnitOfWork) -> str:
    """What publishing decides on: the page's text, as stored.

    Raises:
        NotFound: The page is gone.
    """
    if command.page_id not in uow.texts:
        raise NotFound("No such page.")
    return uow.texts[command.page_id]


def show_page(command: PublishPage, uow: PagesUnitOfWork) -> Shown:
    """The page's text, as the user confirms it."""
    return Shown("Publish a page", {"Text": uow.texts[command.page_id]})


PUBLISH_PAGE = spent_in_code(
    Action(
        "pages.publish",
        command=PublishPage,
        subject=lambda command: f"pages.page:{command.page_id}",
        show=show_page,
        binds=bound_text,
    )
)


@requires_authorization(PUBLISH_PAGE)
async def publish(command: PublishPage, uow: PagesUnitOfWork) -> None:
    """Publish the page as it reads once the unit of work is open."""
    async with uow:
        PUBLISHED.append((command.page_id, uow.texts[command.page_id]))
        await uow.commit()


@requires_authorization(PUBLISH_PAGE)
async def publish_committing(command: PublishPage, uow: PagesUnitOfWork) -> None:
    """Commit without opening the unit of work, then publish the page."""
    await uow.commit()
    PUBLISHED.append((command.page_id, PAGES[command.page_id]))


@requires_authorization(PUBLISH_PAGE)
async def publish_unopened(command: PublishPage, uow: PagesUnitOfWork) -> None:
    """Publish the page without opening the unit of work: a mistake."""
    PUBLISHED.append((command.page_id, PAGES[command.page_id]))


router = APIRouter()
router.include_router(auth_router, prefix="/auth")
router.include_router(mfa_router, prefix="/auth")
router.include_router(actions_router, prefix="/auth")
PUBLISHING = [Depends(Require(StepUp(for_=PUBLISH_PAGE)))]


@router.post("/pages/{page_id}/publish", dependencies=PUBLISHING)
async def publish_page(page_id: UUID, user: AuthenticatedUser) -> Response:
    """Publish the page, with an authorization of it as it read."""
    await publish(PublishPage(page_id=page_id), PagesUnitOfWork())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/pages/{page_id}/publish/in-code", dependencies=PUBLISHING)
async def publish_page_in_code(page_id: UUID, user: AuthenticatedUser) -> Response:
    """Publish the page in code that spends the authorization itself."""
    command = PublishPage(page_id=page_id)
    uow = PagesUnitOfWork()
    async with uow:
        await spend_authorization(uow, PUBLISH_PAGE, command)
        PUBLISHED.append((page_id, uow.texts[page_id]))
        await uow.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/pages/{page_id}/publish/committed", dependencies=PUBLISHING)
async def publish_page_committed(page_id: UUID, user: AuthenticatedUser) -> Response:
    """Publish the page through a service that commits without opening first."""
    await publish_committing(PublishPage(page_id=page_id), PagesUnitOfWork())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/pages/{page_id}/publish/unopened", dependencies=PUBLISHING)
async def publish_page_unopened(page_id: UUID, user: AuthenticatedUser) -> Response:
    """Publish the page through a service that never opens its unit of work."""
    await publish_unopened(PublishPage(page_id=page_id), PagesUnitOfWork())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/pages/{page_id}/publish/no-unit", dependencies=PUBLISHING)
async def publish_page_without_unit(page_id: UUID, user: AuthenticatedUser) -> Response:
    """Publish the page through a service given no unit of work."""
    await publish(PublishPage(page_id=page_id), None)  # type: ignore[arg-type]
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/pages/{page_id}/publish/authorization", status_code=201)
async def ask_to_publish(
    page_id: UUID, request: Request, user: AuthenticatedUser
) -> ActionAuthorizationOut:
    """Ask for an authorization of publishing the page, read where the act reads."""
    command = PublishPage(page_id=page_id)
    asked = await request_action_authorization(
        request, PUBLISH_PAGE, command, unit_of_work=PagesUnitOfWork
    )
    return ActionAuthorizationOut.of(asked)
