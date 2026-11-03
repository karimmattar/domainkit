"""OpenID Connect and OAuth 2 providers: where to send the browser, and what came back.

Every provider makes an authorization URL with PKCE (S256) and a state,
and trades the code that comes back for the account it proves
(``Claims``). An OpenID Connect provider's ID token is verified here: its
signature by a key of the provider's JWKS (an asymmetric algorithm
only), its issuer, audience (this client), expiry, and nonce. GitHub,
which speaks OAuth 2 alone, is asked for the user and their verified
primary email with the access token.

The discovery document and the keys are read through
``domainkit.http.client`` and kept for an hour; a key id not seen before
reads the keys again, once, so a provider's key rotation never refuses
a login.
"""

import base64
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

from domainkit.exceptions import ImproperlyConfigured

from .exceptions import ProviderError
from .settings import ProviderSettings

__all__ = [
    "AppleProvider",
    "Claims",
    "GitHubProvider",
    "OIDCProvider",
    "Provider",
    "make_provider",
]

ALGORITHMS: Final = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
)
"""ID token algorithms believed: asymmetric only, never ``none`` or HMAC."""
KEEP: Final = 3600.0
"""Seconds a discovery document or a key set is kept."""
LEEWAY: Final = 60
"""Seconds of clock skew allowed on an ID token's times."""


@dataclass(frozen=True, slots=True)
class Claims:
    """Who the provider says logged in: its subject, and an email if it gave one."""

    subject: str
    email: str | None = None
    email_verified: bool = False


def pkce_challenge(verifier: str) -> str:
    """PKCE's S256 challenge of ``verifier``."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class Provider:
    """A provider of identities; subclasses speak its protocol."""

    def __init__(self, name: str, config: ProviderSettings) -> None:
        """Use the client ``config`` describes, under ``name``."""
        self.name = name
        self.config = config
        from domainkit.http.client import HttpClient

        self.client = HttpClient(propagate=False)

    async def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        """Where to send the browser to log in."""
        raise NotImplementedError  # pragma: no cover - every provider says.

    async def exchange(self, code: str, verifier: str, nonce: str) -> Claims:
        """The account the code proves.

        Raises:
            ProviderError: The provider refused, or answered what does not hold.
        """
        raise NotImplementedError  # pragma: no cover - every provider says.

    def client_secret(self) -> str | None:
        """The secret the token request sends."""
        secret = self.config.CLIENT_SECRET
        return None if secret is None else secret.get_secret_value()

    async def _post_token(self, url: str, code: str, verifier: str) -> dict[str, Any]:
        """The token endpoint's answer to the authorization code grant."""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.config.REDIRECT_URI,
            "client_id": self.config.CLIENT_ID,
            "code_verifier": verifier,
        }
        if (secret := self.client_secret()) is not None:
            form["client_secret"] = secret
        response = await self._call(
            "POST", url, data=form, headers={"Accept": "application/json"}
        )
        if response.status_code != 200:
            raise ProviderError(
                f"{self.name} refused the code ({response.status_code})."
            )
        return _json(response, self.name)

    async def _call(self, method: str, url: str, **options: Any) -> Any:  # noqa: ANN401
        """One call to the provider.

        Raises:
            ProviderError: It could not be reached.
        """
        import httpx2

        try:
            return await self.client.request(method, url, **options)
        except httpx2.HTTPError as exc:
            raise ProviderError(f"{self.name} could not be reached.") from exc


class OIDCProvider(Provider):
    """An OpenID Connect provider, found by its issuer's discovery document."""

    def issuer(self) -> str:
        """The issuer ID tokens must name.

        Raises:
            ImproperlyConfigured: The provider has no ``ISSUER``.
        """
        if not self.config.ISSUER:
            raise ImproperlyConfigured(
                f"IDENTITY.PROVIDERS[{self.name!r}].ISSUER is unset."
            )
        return self.config.ISSUER.rstrip("/")

    async def discovery(self) -> dict[str, Any]:
        """The issuer's discovery document, kept for an hour."""
        issuer = self.issuer()
        held = _documents.get(issuer)
        if held is not None and held[1] > time.monotonic():
            return held[0]
        response = await self._call("GET", f"{issuer}/.well-known/openid-configuration")
        if response.status_code != 200:
            status_code = response.status_code
            raise ProviderError(
                f"{self.name}'s discovery document is unavailable ({status_code})."
            )
        document = _json(response, self.name)
        if document.get("issuer", "").rstrip("/") != issuer:
            raise ProviderError(
                f"{self.name}'s discovery document names another issuer."
            )
        _documents[issuer] = (document, time.monotonic() + KEEP)
        return document

    async def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        """The authorization endpoint, with the code flow, PKCE, state, and nonce."""
        endpoint = (await self.discovery())["authorization_endpoint"]
        query = {
            "response_type": "code",
            "client_id": self.config.CLIENT_ID,
            "redirect_uri": self.config.REDIRECT_URI,
            "scope": " ".join(self.config.SCOPES),
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{endpoint}?{urlencode(query)}"

    async def exchange(self, code: str, verifier: str, nonce: str) -> Claims:
        """Trade the code for an ID token, and verify it."""
        document = await self.discovery()
        tokens = await self._post_token(document["token_endpoint"], code, verifier)
        raw = tokens.get("id_token")
        if not isinstance(raw, str):
            raise ProviderError(f"{self.name} answered without an ID token.")
        claims = await self.verify(raw, nonce)
        verified = claims.get("email_verified")
        return Claims(
            subject=str(claims["sub"]),
            email=claims.get("email") if isinstance(claims.get("email"), str) else None,
            email_verified=verified is True or verified == "true",
        )

    async def verify(self, raw: str, nonce: str) -> dict[str, Any]:
        """The claims of an ID token this provider signed for this client.

        Raises:
            ProviderError: Any check fails; why is logged, not told.
        """
        import jwt

        try:
            header = jwt.get_unverified_header(raw)
        except jwt.PyJWTError:
            raise ProviderError(f"{self.name}'s ID token is malformed.") from None
        algorithm = header.get("alg")
        if algorithm not in ALGORITHMS:
            raise ProviderError(f"{self.name}'s ID token uses {algorithm!r}, refused.")
        key = await self._key(header.get("kid"))
        try:
            claims: dict[str, Any] = jwt.decode(
                raw,
                key=key,
                algorithms=[algorithm],
                audience=self.config.CLIENT_ID,
                issuer=self.expected_issuer(),
                leeway=LEEWAY,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise ProviderError(
                f"{self.name}'s ID token does not hold: {exc}."
            ) from None
        if claims.get("nonce") != nonce:
            raise ProviderError(
                f"{self.name}'s ID token answers another login (nonce)."
            )
        return claims

    def expected_issuer(self) -> str:
        """The ``iss`` an ID token must carry."""
        return self.issuer()

    async def _key(self, kid: object) -> Any:  # noqa: ANN401 - a cryptography key.
        """The provider's key ``kid``; an unknown one reads the key set again, once."""
        import jwt

        uri = (await self.discovery())["jwks_uri"]
        for fresh in (False, True):
            keys = await self._keys(uri, fresh=fresh)
            for jwk in keys:
                if kid is None or jwk.get("kid") == kid:
                    try:
                        return jwt.PyJWK(jwk).key
                    except jwt.PyJWTError:
                        continue
        raise ProviderError(f"{self.name} signed with a key it does not publish.")

    async def _keys(self, uri: str, *, fresh: bool) -> list[dict[str, Any]]:
        """The key set at ``uri``, kept for an hour unless ``fresh``."""
        held = _key_sets.get(uri)
        if not fresh and held is not None and held[1] > time.monotonic():
            return held[0]
        response = await self._call("GET", uri)
        if response.status_code != 200:
            raise ProviderError(
                f"{self.name}'s keys are unavailable ({response.status_code})."
            )
        keys = _json(response, self.name).get("keys")
        if not isinstance(keys, list):
            raise ProviderError(f"{self.name} published no keys.")
        _key_sets[uri] = (keys, time.monotonic() + KEEP)
        return keys


class GoogleProvider(OIDCProvider):
    """Google accounts (OpenID Connect)."""

    def issuer(self) -> str:
        """Google's issuer."""
        return "https://accounts.google.com"


class MicrosoftProvider(OIDCProvider):
    """Microsoft Entra ID accounts of one tenant (OpenID Connect)."""

    def issuer(self) -> str:
        """The tenant's issuer (v2.0 endpoints).

        Raises:
            ImproperlyConfigured: No ``TENANT`` is set.
        """
        if not self.config.TENANT:
            raise ImproperlyConfigured(
                f"IDENTITY.PROVIDERS[{self.name!r}].TENANT names the Entra ID tenant."
            )
        return f"https://login.microsoftonline.com/{self.config.TENANT}/v2.0"


class AppleProvider(OIDCProvider):
    """Sign in with Apple: its client secret is a JWT this application signs."""

    def issuer(self) -> str:
        """Apple's issuer."""
        return "https://appleid.apple.com"

    def client_secret(self) -> str:
        """An ES256 JWT, valid five minutes, signed by the team's key.

        Raises:
            ImproperlyConfigured: A part of the key is unset.
        """
        import jwt

        config = self.config
        if not (
            config.APPLE_TEAM_ID and config.APPLE_KEY_ID and config.APPLE_PRIVATE_KEY
        ):
            raise ImproperlyConfigured(
                f"IDENTITY.PROVIDERS[{self.name!r}] needs APPLE_TEAM_ID, "
                "APPLE_KEY_ID, and APPLE_PRIVATE_KEY."
            )
        now = int(time.time())
        return jwt.encode(
            {
                "iss": config.APPLE_TEAM_ID,
                "iat": now,
                "exp": now + 300,
                "aud": self.issuer(),
                "sub": config.CLIENT_ID,
            },
            config.APPLE_PRIVATE_KEY.get_secret_value(),
            algorithm="ES256",
            headers={"kid": config.APPLE_KEY_ID},
        )


class GitHubProvider(Provider):
    """GitHub accounts (OAuth 2): the user, and their verified primary email."""

    AUTHORIZE: Final = "https://github.com/login/oauth/authorize"
    TOKEN: Final = "https://github.com/login/oauth/access_token"  # noqa: S105 - a URL.
    API: Final = "https://api.github.com"

    async def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        """GitHub's authorize page, with PKCE and the state (no nonce: no ID token)."""
        query = {
            "client_id": self.config.CLIENT_ID,
            "redirect_uri": self.config.REDIRECT_URI,
            "scope": "read:user user:email",
            "state": state,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{self.AUTHORIZE}?{urlencode(query)}"

    async def exchange(self, code: str, verifier: str, nonce: str) -> Claims:
        """Trade the code for a token; ask GitHub who it is."""
        tokens = await self._post_token(self.TOKEN, code, verifier)
        token = tokens.get("access_token")
        if not isinstance(token, str):
            raise ProviderError(f"{self.name} answered without a token.")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        user = await self._call("GET", f"{self.API}/user", headers=headers)
        emails = await self._call("GET", f"{self.API}/user/emails", headers=headers)
        if user.status_code != 200 or emails.status_code != 200:
            raise ProviderError(f"{self.name} would not say who logged in.")
        primary = next(
            (
                entry
                for entry in emails.json()
                if isinstance(entry, dict) and entry.get("primary")
            ),
            {},
        )
        return Claims(
            subject=str(_json(user, self.name)["id"]),
            email=primary.get("email"),
            email_verified=primary.get("verified") is True,
        )


_KINDS: Final[dict[str, type[Provider]]] = {
    "oidc": OIDCProvider,
    "google": GoogleProvider,
    "microsoft": MicrosoftProvider,
    "apple": AppleProvider,
    "github": GitHubProvider,
}
_documents: dict[str, tuple[dict[str, Any], float]] = {}
_key_sets: dict[str, tuple[list[dict[str, Any]], float]] = {}


def make_provider(name: str, config: ProviderSettings) -> Provider:
    """The provider ``config`` describes."""
    return _KINDS[config.KIND](name, config)


def forget() -> None:
    """Drop the documents and keys kept (tests)."""
    _documents.clear()
    _key_sets.clear()


def _json(response: Any, name: str) -> dict[str, Any]:  # noqa: ANN401 - httpx2's.
    """An answer's JSON object.

    Raises:
        ProviderError: It is not one.
    """
    try:
        document = response.json()
    except ValueError:
        raise ProviderError(f"{name} answered something that is not JSON.") from None
    if not isinstance(document, dict):
        raise ProviderError(f"{name} answered something that is not a JSON object.")
    return document
