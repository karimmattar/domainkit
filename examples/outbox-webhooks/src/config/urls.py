"""The project's routes: mount each domain's router here.

Every route must say who may call it: ``Depends(Require(...))``, or
``dependencies=[Public]`` for anyone. ``manage.py check`` lists those that
do not, and staging and production refuse to start with any.
"""

from domainkit.contrib.auth.admin import router as admin_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.webhooks.admin import router as webhooks_router
from domainkit.http.health import router as health_router
from fastapi import APIRouter

from domains.transfers.routes import router as transfers_router

router = APIRouter()
router.include_router(health_router)
router.include_router(auth_router, prefix="/auth")
router.include_router(admin_router, prefix="/admin/auth")
# Staff subscribe other services' URLs to the events transfers expose.
router.include_router(webhooks_router, prefix="/admin/webhooks")
router.include_router(transfers_router, prefix="/api/v1")
