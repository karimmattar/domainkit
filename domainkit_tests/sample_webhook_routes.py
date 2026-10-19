"""The webhooks admin API, mounted as a project would."""

from fastapi import APIRouter

from domainkit.contrib.webhooks.admin import router as webhooks_admin

router = APIRouter()
router.include_router(webhooks_admin, prefix="/admin/webhooks")
