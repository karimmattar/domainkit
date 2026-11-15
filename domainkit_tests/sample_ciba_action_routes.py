"""The routes of the phone's action authorization tests (user report A6).

Ciba's sample routes, auth's routes that confirm an authorization, and
one act: renaming oneself (``sample_action_routes.RENAME``), which asks
``StepUp(for_=...)``, with its twin that asks for an authorization.
"""

from fastapi import APIRouter, Depends, Request, status

from domainkit.contrib.auth import AuthenticatedUser, AuthUnitOfWork
from domainkit.contrib.auth.actions import (
    ActionAuthorizationOut,
    request_action_authorization,
)
from domainkit.contrib.auth.actions import router as actions_router
from domainkit.http.access import Require
from domainkit_tests import sample_action_routes as acts
from domainkit_tests.sample_ciba_routes import router as ciba_router

__all__ = ["router"]

router = APIRouter()
router.include_router(ciba_router)
router.include_router(actions_router, prefix="/auth")


@router.post("/me/name", dependencies=[Depends(Require(acts.RENAMING))])
async def change_name(body: acts.NameIn, user: AuthenticatedUser) -> dict[str, str]:
    """Rename oneself, with an authorization of that very name."""
    command = acts.Rename(user_id=user.id, first_name=body.first_name)
    return {"first_name": await acts.rename(command, AuthUnitOfWork())}


@router.post("/me/name/authorization", status_code=status.HTTP_201_CREATED)
async def ask_to_rename(
    body: acts.NameIn, request: Request, user: AuthenticatedUser
) -> ActionAuthorizationOut:
    """Ask for an authorization of renaming oneself ``first_name``."""
    command = acts.Rename(user_id=user.id, first_name=body.first_name)
    asked = await request_action_authorization(request, acts.RENAME, command)
    return ActionAuthorizationOut.of(asked)
