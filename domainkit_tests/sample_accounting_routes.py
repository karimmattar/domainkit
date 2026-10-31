"""The ledger's API, mounted as a project would."""

from fastapi import APIRouter

from domainkit.contrib.accounting.api import router as accounting

router = APIRouter()
router.include_router(accounting, prefix="/accounting")
