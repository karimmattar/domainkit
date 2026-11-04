"""A route that pays once per call, in the request's tenant (idempotency tests)."""

from typing import Any

from fastapi import APIRouter

from domainkit.contrib.auth import AuthenticatedUser
from domainkit.ddd.tenancy import current_tenant

router = APIRouter()
RAN: list[str | None] = []
"""The tenant of each payout that ran."""


@router.post("/payouts", status_code=201)
async def payout(body: dict[str, Any], user: AuthenticatedUser) -> dict[str, Any]:
    """Pay, and say in which tenant."""
    RAN.append(current_tenant())
    return {"tenant": current_tenant(), "amount": body["amount"]}
