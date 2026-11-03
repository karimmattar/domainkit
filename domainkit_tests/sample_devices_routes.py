"""The routes of the device tests: auth, its second factor, devices, and two guarded."""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request

from domainkit.contrib.auth.access import StepUp
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.mfa import router as mfa_router
from domainkit.contrib.auth.stepup import router as step_up_router
from domainkit.contrib.devices import DeviceSigned
from domainkit.contrib.devices.api import router as devices_router
from domainkit.http.access import Require
from domainkit.http.health import router as health_router

router = APIRouter()
router.include_router(health_router)
for included in (auth_router, mfa_router, step_up_router, devices_router):
    router.include_router(included, prefix="/auth")


@router.post("/payees", dependencies=[Depends(Require(StepUp(timedelta(minutes=5))))])
async def add_payee() -> dict[str, str]:
    """An act a step-up guards."""
    return {"added": "payee"}


@router.post("/transfers", dependencies=[Depends(Require(DeviceSigned()))])
async def transfer(request: Request) -> dict[str, Any]:
    """An act only a bound device may ask for: the body it signed, read again."""
    return {"sent": await request.json()}
