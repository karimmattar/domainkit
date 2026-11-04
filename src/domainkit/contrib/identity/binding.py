"""A login or link started in one browser is finished in that browser only.

A flow's ``state`` (OAuth 2, OpenID Connect) or ``RelayState`` (SAML) says
which flow a provider's answer belongs to, not who started it: an answer
of the attacker's own flow, finished in the victim's browser, logged the
victim into the attacker's account, or linked the victim's identity to it.
So the flow is bound to the browser that started it: OAuth 2's start
answers a ``binding`` the page keeps and sends back with the callback;
SAML's, which the browser follows itself, sets a cookie that the ACS and
the ticket's redemption read. Both are a signature of the flow's id
(``SECRET_KEY``): nothing is stored.
"""

from typing import Final

from domainkit.signing import Signer, constant_time_compare

__all__ = ["COOKIE", "TICKET_SEPARATOR", "binding", "bound"]

SALT: Final = "domainkit.identity.flow"
"""What a flow's binding is signed for: no other signed value verifies."""
COOKIE: Final = "__Host-dk_identity"
"""The cookie SAML's start sets: the binding of the browser's flow."""
TICKET_SEPARATOR: Final = "."
"""Between a SAML ticket and the flow it ends, in what the page redeems."""


def binding(flow: str) -> str:
    """The binding of the flow ``flow``: what its browser holds."""
    return Signer(salt=SALT).signature(flow)


def bound(flow: str, given: str | None) -> bool:
    """Whether ``given`` is the binding of ``flow``, compared in constant time.

    Under the signing key only: a flow lasts minutes (``FLOW_TTL``), so one
    started before ``SECRET_KEY`` changed is started again.
    """
    return bool(given) and constant_time_compare(binding(flow), given or "")
