"""Acts confirmed by a bound device or a passkey, for the tests (user report A6).

The routes of ``sample_action_routes`` (auth's, the confirmations by code,
and renaming oneself with an authorization of that very name), and the
routes of passkeys and bound devices.
"""

from fastapi import APIRouter

from domainkit.contrib.devices.api import router as devices_router
from domainkit_tests import sample_action_routes as acts

__all__ = ["router"]

router = APIRouter()
router.include_router(acts.router)
router.include_router(devices_router, prefix="/auth")
