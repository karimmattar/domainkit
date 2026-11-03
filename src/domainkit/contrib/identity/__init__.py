"""Logging in through identity providers: OpenID Connect, OAuth 2, SAML 2.0, LDAP.

Install it after ``domainkit.contrib.auth`` (and the ``identity`` extra),
name the providers, and mount its routes::

    INSTALLED_DOMAINS = [..., "domainkit.contrib.auth", "domainkit.contrib.identity"]
    IDENTITY = {
        "PROVIDERS": {
            "google": {
                "KIND": "google",
                "CLIENT_ID": env.str("GOOGLE_CLIENT_ID"),
                "CLIENT_SECRET": env.str("GOOGLE_CLIENT_SECRET"),
                "REDIRECT_URI": "https://app.example.com/login/google",
            },
            "keycloak": {
                "ISSUER": "https://sso.example.com/realms/staff",
                "CLIENT_ID": "billing",
                "CLIENT_SECRET": env.str("KEYCLOAK_SECRET"),
                "REDIRECT_URI": "https://app.example.com/login/keycloak",
            },
        },
    }

A user logs in at a provider and comes back with tokens of this
application, as a password login gives (with the second factor's
challenge, if they have one). Identities are linked to users by the
provider's subject, never by email: an account whose email a provider's
account shares is not handed to it (see ``services``). See
``docs/auth/identity.md``.
"""

from .exceptions import (
    EmailTaken,
    IdentityTaken,
    LastWayIn,
    NoAccount,
    ProviderError,
    UnknownProvider,
)
from .providers import Claims
from .stores import IdentityUnitOfWork, LinkedIdentity

__all__ = [
    "Claims",
    "EmailTaken",
    "IdentityTaken",
    "IdentityUnitOfWork",
    "LastWayIn",
    "LinkedIdentity",
    "NoAccount",
    "ProviderError",
    "UnknownProvider",
]
