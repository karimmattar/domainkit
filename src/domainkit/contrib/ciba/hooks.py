"""Hook points of the project's own sign-in pages (user report A8).

A domain registers on them in ``ready()``, as on any hook point::

    from domainkit.contrib.ciba.hooks import on_approve, on_start

    async def name_the_dashboard(request: Request) -> str | None:
        if request.headers.get("origin") == DASHBOARD:
            return "Sign in to the dashboard"
        return None

    async def staff_only(request, user, sign_in) -> None:
        if sign_in.binding_message == "Sign in to the dashboard" and not user.is_staff:
            raise PermissionDenied("Only staff sign in to the dashboard.")

    on_start(name_the_dashboard)
    on_approve(staff_only)
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from starlette.requests import Request

from domainkit.apps.hooks import HookPoint

if TYPE_CHECKING:
    from .stores import SignInRequest

__all__ = ["on_approve", "on_start"]

type StartHook = Callable[[Request], Awaitable[str | None]]
type ApproveHook = Callable[[Request, Any, "SignInRequest"], Awaitable[None]]

on_start: HookPoint[StartHook] = HookPoint("ciba_start", arguments=1, awaited=True)
"""A sign-in the project's own page starts (push or QR): ``(request)``.

Raise to refuse it, as any route's error; return a binding message, shown
on the page's device and on the phone ("Sign in to the dashboard"), or
None. The first message given is kept; every hook runs.
"""
on_approve: HookPoint[ApproveHook] = HookPoint(
    "ciba_approve", arguments=3, awaited=True
)
"""A sign-in the user's phone approves: ``(request, user, sign_in)``.

Called before the approval is recorded, for a request of the user's own;
raise to refuse it (as ``PermissionDenied``): nothing is approved.
"""
