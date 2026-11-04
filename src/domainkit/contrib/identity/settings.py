"""Settings of ``domainkit.contrib.identity``: ``IDENTITY = {...}``."""

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

type ProviderKind = Literal["oidc", "google", "microsoft", "apple", "github"]


class ProviderSettings(BaseModel):
    """One identity provider: who it is, and this application's client there."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    KIND: ProviderKind = "oidc"
    """``oidc`` for any OpenID Connect provider by its ``ISSUER`` (Keycloak,
    Okta, Auth0, AWS Cognito, Huawei IAM); ``google``, ``microsoft``,
    ``apple``, and ``github`` know their own endpoints."""
    CLIENT_ID: str = Field(min_length=1)
    CLIENT_SECRET: SecretStr | None = None
    """The client's secret; Apple's is made from ``APPLE_*`` instead."""
    REDIRECT_URI: str = Field(min_length=1)
    """Your page the provider sends the browser back to, with the code."""
    ISSUER: str | None = None
    """The OpenID Connect issuer (``oidc``): its discovery document is read."""
    TENANT: str | None = None
    """Microsoft: the Entra ID tenant id whose accounts may log in."""
    SCOPES: tuple[str, ...] = ("openid", "email", "profile")
    APPLE_TEAM_ID: str | None = None
    APPLE_KEY_ID: str | None = None
    APPLE_PRIVATE_KEY: SecretStr | None = None
    """Apple's ES256 key (PEM), which signs the client secret."""


class SamlSettings(BaseModel):
    """One SAML 2.0 identity provider, and this application as its service provider."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    ENTITY_ID: str = Field(min_length=1)
    """This application's entity id at the provider."""
    ACS_URL: str = Field(min_length=1)
    """Where the provider posts its response: ``.../identity/saml/<name>/acs``."""
    IDP_ENTITY_ID: str = Field(min_length=1)
    IDP_SSO_URL: str = Field(min_length=1)
    """The provider's single sign-on URL (HTTP-Redirect binding)."""
    IDP_CERTIFICATE: str = Field(min_length=1)
    """The provider's signing certificate (PEM): only what it signs is believed."""
    EMAIL_ATTRIBUTE: str | None = None
    """The attribute holding the email address; None: the NameID, if an email."""


class LdapSettings(BaseModel):
    """A directory users log in with: LDAP, or Active Directory."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    URL: str = Field(min_length=1)
    """``ldaps://dc.example.com`` (or ``ldap://`` with ``START_TLS``)."""
    START_TLS: bool = False
    CA_CERTIFICATES: str | None = None
    """A file of the certificates that sign the directory's; None: the system's.

    The directory's certificate is always checked, and its name too.
    """
    BIND_DN: str | None = None
    """The account that looks users up; None: an anonymous search."""
    BIND_PASSWORD: SecretStr | None = None
    USER_SEARCH_BASE: str = Field(min_length=1)
    USER_FILTER: str = "(uid={username})"
    """``(sAMAccountName={username})`` for Active Directory; escaped."""
    EMAIL_ATTRIBUTE: str = "mail"
    GROUPS: dict[str, str] = Field(default_factory=dict)
    """A directory group's DN, and the group a member is put in here."""
    TIMEOUT: float = Field(default=10.0, gt=0, le=60)


class IdentitySettings(BaseModel):
    """Identity providers users log in with; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    PROVIDERS: dict[str, ProviderSettings] = Field(default_factory=dict)
    """OpenID Connect and OAuth 2 providers, by the name routes use."""
    SAML: dict[str, SamlSettings] = Field(default_factory=dict)
    """SAML 2.0 providers, by name."""
    LDAP: LdapSettings | None = None
    """The directory ``LDAPBackend`` checks passwords against."""
    SIGNUP: bool = False
    """Make an account for a new identity whose email the provider verified."""
    FLOW_TTL: timedelta = Field(default=timedelta(minutes=10), gt=timedelta(0))
    """How long a login started at a provider may take to come back."""
    TICKET_TTL: timedelta = Field(
        default=timedelta(minutes=1), gt=timedelta(0), le=timedelta(minutes=10)
    )
    """How long a ticket from a posted response (SAML) may wait to be redeemed."""
    FRONTEND_URL: str | None = None
    """Your page a posted response sends the browser to: ``?ticket=`` added."""
