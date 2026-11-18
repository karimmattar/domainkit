"""The project's routes: mount each domain's router here.

Every route must say who may call it: ``Depends(Require(...))``, or
``dependencies=[Public]`` for anyone. ``manage.py check`` lists those that
do not, and staging and production refuse to start with any.
"""

from domainkit.contrib.accounting.api import router as accounting_router
from domainkit.contrib.auth.admin import router as admin_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.http.health import router as health_router
from fastapi import APIRouter

from domains.wallets.routes import router as wallets_router

router = APIRouter()
router.include_router(health_router)
router.include_router(auth_router, prefix="/auth")
router.include_router(admin_router, prefix="/admin/auth")
# The customers' wallets, and the clerks' deposits and holds.
router.include_router(wallets_router, prefix="/api/v1")
# The back office: the books, behind the accounting app's model permissions
# (refunds are reversals; the trial balance is a period's report).
router.include_router(accounting_router, prefix="/api/accounting")
