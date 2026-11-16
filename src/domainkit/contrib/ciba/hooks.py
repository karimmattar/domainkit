"""Hook points of the project's own sign-in pages (user report A8).

A domain registers on them in ``ready()``, as on any hook point::

    from domainkit.contrib.ciba.hooks import SignInPolicy, on_approve, on_start

    async def dashboard_policy(request: Request) -> SignInPolicy | None:
        if request.headers.get("origin") == DASHBOARD:
            return SignInPolicy(
                binding_message="Sign in to the dashboard",
                number_matching=True,
                same_network="require",
            )
        return None

    async def staff_only(request, user, sign_in) -> None:
        if sign_in.binding_message == "Sign in to the dashboard" and not user.is_staff:
            raise PermissionDenied("Only staff sign in to the dashboard.")

    on_start(dashboard_policy)
    on_approve(staff_only)
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from starlette.requests import Request

from domainkit.apps.hooks import HookPoint

if TYPE_CHECKING:
    from .stores import SignInRequest

__all__ = ["SignInPolicy", "on_approve", "on_start"]


@dataclass(frozen=True, slots=True)
class SignInPolicy:
    """What a page's sign-in asks of the phone; a field left None keeps the setting.

    An ``on_start`` hook returns one in place of a binding message, to ask
    more, or less, of one page than of the others (ADR 0155).
    """

    binding_message: str | None = None
    """Shown on the page and on the phone: 1 to 64 printable characters."""
    number_matching: bool | None = None
    """Whether the phone types the two digits the page shows. None:
    ``NUMBER_MATCHING`` for a push, ``QR_NUMBER_MATCHING`` for a QR code."""
    same_network: Literal["warn", "require"] | None = None
    """What a phone on another address than the page gets when it claims
    the page's QR code: a warning, or a refusal. None: ``QR_SAME_NETWORK``.
    A push has no code to claim."""


type StartHook = Callable[[Request], Awaitable[str | SignInPolicy | None]]
type ApproveHook = Callable[[Request, Any, "SignInRequest"], Awaitable[None]]

on_start: HookPoint[StartHook] = HookPoint("ciba_start", arguments=1, awaited=True)
"""A sign-in the project's own page starts (push or QR): ``(request)``.

Raise to refuse it, as any route's error. Return a binding message, shown
on the page's device and on the phone ("Sign in to the dashboard"), a
``SignInPolicy``, or None. Every hook runs. The first message given is
kept; number matching is asked if a hook asks it, and the same network
required if a hook requires it: a hook relaxes a setting only where no
other asks more.
"""
on_approve: HookPoint[ApproveHook] = HookPoint(
    "ciba_approve", arguments=3, awaited=True
)
"""A sign-in the user's phone approves: ``(request, user, sign_in)``.

Called before the approval is recorded, for a request of the user's own;
raise to refuse it (as ``PermissionDenied``): nothing is approved.
"""
