"""Limits' and the ledger's APIs, mounted as a project would."""

from fastapi import APIRouter

from domainkit.contrib.accounting.api import router as accounting
from domainkit.contrib.limits.api import router as limits

router = APIRouter()
router.include_router(accounting, prefix="/accounting")
router.include_router(limits, prefix="/limits")
