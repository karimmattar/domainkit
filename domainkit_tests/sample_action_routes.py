"""Acts authorized one at a time, for the tests of ``auth.actions`` (user report A6).

Two acts: renaming oneself, on auth's database (spent with the act), and
archiving a note, kept in memory (spent before the act). Each route asks
``StepUp(for_=...)``; each has a twin that asks for an authorization, as a
project writes for a plain route. ``FAILURES`` makes the next acts fail
before they commit; ``BARRIER`` holds two acts until both are there.
"""

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel

from domainkit.contrib.auth import AuthenticatedUser, AuthUnitOfWork, StepUp, UserId
from domainkit.contrib.auth.actions import (
    Action,
    ActionAuthorizationOut,
    Shown,
    request_action_authorization,
    requires_authorization,
    spend_authorization,
)
from domainkit.contrib.auth.actions import router as actions_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.mfa import router as mfa_router
from domainkit.ddd import Command, UnitOfWork
from domainkit.http.access import AsksAuthorization, Require
from domainkit.resources.routing.reach import Joined

__all__ = ["ARCHIVE", "RENAME", "router"]


class Rename(Command):
    """Change the user's first name."""

    user_id: UUID
    first_name: str
    asked_at: str = ""
    """Changes with every call: left out of the digest."""


class ArchiveNote(Command):
    """Archive one of the user's notes."""

    user_id: UUID
    note_id: UUID


async def show_rename(command: Rename) -> Shown:
    """What the user confirms: the new name."""
    return Shown("Change your first name", {"New first name": command.first_name})


def show_archive(command: ArchiveNote) -> Shown:
    """What the user confirms: the note."""
    return Shown("Archive a note", [("Note", str(command.note_id))])


RENAME = Action(
    "accounts.rename",
    command=Rename,
    subject=lambda command: f"auth.user:{command.user_id}",
    show=show_rename,
    ignore=("asked_at",),
)
ARCHIVE = Action(
    "notes.archive",
    command=ArchiveNote,
    subject=lambda command: f"notes.note:{command.note_id}",
    show=show_archive,
)

FAILURES: list[Exception] = []
"""What the next acts raise before they commit, one each."""
ARCHIVED: list[UUID] = []
"""The notes archived."""
BARRIER: list[asyncio.Barrier] = []
"""A barrier the acts wait at before they commit, if one is set."""


async def _hold() -> None:
    """Fail as ``FAILURES`` says; wait at ``BARRIER`` (5 seconds at most)."""
    if FAILURES:
        raise FAILURES.pop(0)
    if BARRIER:
        try:
            async with asyncio.timeout(5):
                await BARRIER[0].wait()
        except TimeoutError:
            pass


async def rename_unspent(command: Rename, uow: Any) -> str:  # noqa: ANN401 - a uow.
    """Rename the user, spending nothing: a service left undecorated."""
    async with uow:
        user = await uow.users.get(UserId(command.user_id))
        user.first_name = command.first_name
        await _hold()
        await uow.commit()
    return command.first_name


rename = requires_authorization(RENAME)(rename_unspent)
"""The act on auth's database: spent with its commit."""


class NotesUnitOfWork(UnitOfWork):
    """Notes kept in memory: not auth's database."""

    async def _commit(self) -> None:
        """Nothing to write."""

    async def _rollback(self) -> None:
        """Nothing to undo."""


@requires_authorization(ARCHIVE)
async def archive(command: ArchiveNote, uow: NotesUnitOfWork) -> None:
    """Archive the note: the authorization is spent before, alone."""
    async with uow:
        await _hold()
        ARCHIVED.append(command.note_id)
        await uow.commit()


class NameIn(BaseModel):
    """A new first name."""

    first_name: str
    asked_at: str = ""


router = APIRouter()
router.include_router(auth_router, prefix="/auth")
router.include_router(mfa_router, prefix="/auth")
router.include_router(actions_router, prefix="/auth")

RENAMING = StepUp(for_=RENAME)
ARCHIVING = StepUp(for_="notes.archive")


@router.post("/me/name", dependencies=[Depends(Require(RENAMING))])
async def change_name(body: NameIn, user: AuthenticatedUser) -> dict[str, str]:
    """Rename oneself, with an authorization of that very name."""
    command = Rename(
        user_id=user.id, first_name=body.first_name, asked_at=body.asked_at
    )
    return {"first_name": await rename(command, AuthUnitOfWork())}


@router.post("/me/name/joined", dependencies=[Depends(Require(RENAMING))])
async def change_name_joined(body: NameIn, user: AuthenticatedUser) -> dict[str, str]:
    """Rename oneself in a unit of work the route opened, as a resource's does."""
    command = Rename(user_id=user.id, first_name=body.first_name)
    uow = AuthUnitOfWork()
    async with uow:
        await uow.users.get(UserId(user.id))  # What a scope's check reads.
        return {"first_name": await rename(command, Joined(uow))}


@router.post("/me/name/in-code", dependencies=[Depends(Require(RENAMING))])
async def change_name_in_code(body: NameIn, user: AuthenticatedUser) -> dict[str, str]:
    """Rename oneself in code that spends the authorization itself."""
    command = Rename(user_id=user.id, first_name=body.first_name)
    async with AuthUnitOfWork() as uow:
        await spend_authorization(uow, RENAME, command)
        stored = await uow.users.get(UserId(user.id))
        stored.first_name = command.first_name  # type: ignore[attr-defined]
        await _hold()
        await uow.commit()
    return {"first_name": command.first_name}


@router.post("/me/name/forgotten", dependencies=[Depends(Require(RENAMING))])
async def change_name_unspent(body: NameIn, user: AuthenticatedUser) -> dict[str, str]:
    """Rename oneself through a service that spends nothing."""
    command = Rename(user_id=user.id, first_name=body.first_name)
    return {"first_name": await rename_unspent(command, AuthUnitOfWork())}


@router.post("/me/name/authorization", status_code=status.HTTP_201_CREATED)
async def ask_to_rename(
    body: NameIn, request: Request, user: AuthenticatedUser
) -> ActionAuthorizationOut:
    """Ask for an authorization of the rename: the rule's own way."""
    command = Rename(
        user_id=user.id, first_name=body.first_name, asked_at=body.asked_at
    )
    assert isinstance(RENAMING, AsksAuthorization)
    asked = await RENAMING.ask_authorization(request, command)
    return ActionAuthorizationOut.model_validate(asked.model_dump())


@router.post(
    "/notes/{note_id}/archive",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(Require(ARCHIVING))],
)
async def archive_note(note_id: UUID, user: AuthenticatedUser) -> Response:
    """Archive a note, with an authorization of that note."""
    await archive(ArchiveNote(user_id=user.id, note_id=note_id), NotesUnitOfWork())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/notes/{note_id}/archive/authorization", status_code=201)
async def ask_to_archive(
    note_id: UUID, request: Request, user: AuthenticatedUser
) -> ActionAuthorizationOut:
    """Ask for an authorization of archiving the note: a function call."""
    command = ArchiveNote(user_id=user.id, note_id=note_id)
    asked = await request_action_authorization(request, ARCHIVE, command)
    return ActionAuthorizationOut.of(asked)
