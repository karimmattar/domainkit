"""How requests prove who sent them (Django REST framework's authentication).

``AUTH.AUTHENTICATION_CLASSES`` lists classes, tried in order by
``AuthenticationMiddleware``; the first to recognize the request's
credentials decides. A class returns None for credentials that are not its
own, and raises ``AuthenticationFailed`` for its own that do not hold.

- ``TokenAuthentication`` (the default): opaque tokens, ``Authorization:
  Bearer dkt_...``, stored as digests and revoked by deleting them.
- ``JWTAuthentication``: short-lived signed access tokens that need no
  lookup, and opaque refresh tokens that renew them (the ``jwt`` extra).

The first class also issues the tokens a login returns. Write a class for
other credentials (a gateway's header, API keys elsewhere)::

    class GatewayAuthentication(BaseAuthentication):
        async def authenticate(self, connection):
            ...  # return Credentials(user, scheme="gateway"), or None
"""

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cache
from typing import Any, ClassVar, Final, cast
from uuid import UUID, uuid4

from fastapi import status
from starlette.requests import HTTPConnection

from domainkit.checks import CheckFailed, CheckMessage
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.errors import APIError
from domainkit.log import SECURITY_LOGGER
from domainkit.utils.module_loading import import_string

from .events import RefreshTokenReused
from .models import AbstractBaseUser, UserId
from .principals import (
    Entry,
    family_target,
    forget,
    kept_or_read,
    principal_cache,
    remember,
    stamps_ahead,
    stamps_before_reading,
    token_target,
    user_target,
)
from .settings import (
    HMAC_ALGORITHMS,
    MAX_RETIREMENT_AHEAD,
    AuthSettings,
    JWTSettings,
    PrincipalCacheSettings,
    auth_settings,
)
from .tokens import (
    LAST_USED_EVERY,
    TokenKind,
    idle_since,
    is_login,
    last_used_every,
    token_digest,
    utc_now,
    well_formed,
)
from .uow import AuthUnitOfWork

__all__ = [
    "AuthenticationFailed",
    "BaseAuthentication",
    "Credentials",
    "JWTAuthentication",
    "TokenAuthentication",
    "active_user",
]

security_logger = logging.getLogger(SECURITY_LOGGER)


@dataclass(frozen=True, slots=True)
class Credentials:
    """Who sent a request, and with what (``request.auth``)."""

    user: AbstractBaseUser
    scheme: str
    """The kind of credentials: ``token``, ``jwt``, or a custom class's."""
    token_id: str | None = None
    """The digest of the opaque token used, which logging out revokes."""
    session: UUID | None = None
    """The login a JWT descends from, whose refresh tokens logging out revokes."""
    scopes: frozenset[str] | None = None
    """The permission keys the credentials are limited to; None: no limit."""

    def allows(self, *perms: str) -> bool:
        """Whether the credentials' scopes cover every one of ``perms``."""
        return self.scopes is None or self.scopes.issuperset(perms)

    def __repr__(self) -> str:
        """The user's id, the scheme, and the scopes: no personal data, no digest."""
        return (
            f"Credentials(user_id={self.user.id!r}, scheme={self.scheme!r}, "
            f"scopes={self.scopes!r})"
        )


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    """What logging in or refreshing returns (OAuth 2's token response).

    Its ``repr`` leaves the tokens out.
    """

    access_token: str = field(repr=False)
    expires_in: int | None
    """Seconds the access token works; None: until revoked."""
    refresh_token: str | None = field(default=None, repr=False)


TOKEN_SCHEME: Final = "token"  # noqa: S105 - a scheme, not a secret.
IDLE: Final = "Session ended after inactivity; log in again."
"""Why a login unused for ``AUTH.IDLE_TIMEOUT`` is refused."""
"""``request.auth.scheme`` of requests an opaque token authenticates."""
JWT_SCHEME: Final = "jwt"
"""``request.auth.scheme`` of requests a JSON Web Token authenticates."""
KEY_ID_LABEL: Final = b"domainkit:jwt-key-id"
"""What an HMAC key's id hashes, keyed with the key itself (ADR 0097)."""
KEY_ID_BYTES: Final = 16
"""How much of that hash an HMAC key's id keeps: 128 bits."""
THUMBPRINT_MEMBERS: Final = {
    "RSA": ("e", "kty", "n"),
    "EC": ("crv", "kty", "x", "y"),
    "OKP": ("crv", "kty", "x"),
}
"""The public members RFC 7638 hashes, per key type, in its (sorted) order."""


class AuthenticationFailed(APIError):
    """Credentials were presented and do not hold (401, unless a subclass says)."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int = status.HTTP_401_UNAUTHORIZED,
        code: str = "authentication_failed",
    ) -> None:
        """Use status 401 and the code ``authentication_failed`` by default."""
        super().__init__(status_code, detail, code=code)


class RefreshNotSupported(APIError):
    """The API's tokens are not renewed with refresh tokens (400)."""

    def __init__(self) -> None:
        """Use status 400."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "Tokens are not refreshed here; log in again.",
            code="refresh_not_supported",
        )


class BaseAuthentication:
    """Recognizes no credentials and issues no tokens; override what you need."""

    scheme: ClassVar[str] = "Bearer"
    """The ``WWW-Authenticate`` scheme clients are told to use."""
    realm: ClassVar[str] = "api"
    guessable: ClassVar[bool] = True
    """Whether anyone may send this class's credentials to guess them.

    While an address is held for refused credentials, credentials of a
    guessable class are refused even when they hold, so a guess that works
    never stands out. Set it False for credentials no client can send at
    will, such as a header only a trusted gateway sets: the address's
    requests that carry them are then served while it is held.
    """

    async def authenticate(self, connection: HTTPConnection) -> Credentials | None:
        """Who sent ``connection``; None if the credentials are not this class's.

        Raises:
            AuthenticationFailed: The credentials are this class's and do not hold.
        """
        return None

    def challenge(self, error: str | None = None) -> str:
        """The ``WWW-Authenticate`` header of a 401 (RFC 6750 errors)."""
        value = f'{self.scheme} realm="{self.realm}"'
        return f'{value}, error="{error}"' if error else value

    async def issue(self, uow: AuthUnitOfWork, user: AbstractBaseUser) -> IssuedTokens:
        """Tokens for ``user``, who just logged in, stored in ``uow``."""
        raise ImproperlyConfigured(
            f"{type(self).__name__} issues no tokens; list a class that does "
            "first in AUTH.AUTHENTICATION_CLASSES."
        )

    async def refresh(self, uow: AuthUnitOfWork, refresh_token: str) -> IssuedTokens:
        """New tokens for a refresh token.

        Raises:
            RefreshNotSupported: This class's tokens are not refreshed.
        """
        raise RefreshNotSupported


class TokenAuthentication(BaseAuthentication):
    """Opaque tokens: ``Authorization: Bearer <AUTH.TOKEN_PREFIX>...``.

    Logging in issues one that works for ``AUTH.TOKEN_TTL``; ``createtoken``
    makes long-lived ones for scripts. A token is looked up by its digest,
    with its user, and kept for ``AUTH.PRINCIPAL_CACHE.TTL``: logging out,
    or a password change, ends it at once in this process, and in the
    others within the TTL (at once with ``STAMPS``). Its expiry is checked
    on every request.
    """

    async def authenticate(self, connection: HTTPConnection) -> Credentials | None:
        """The user of a stored, unexpired access token."""
        raw = bearer_token(connection)
        if raw is None:
            return None
        config = auth_settings()
        prefix = config.TOKEN_PREFIX
        if not raw.startswith(prefix):
            return None
        if not well_formed(raw, prefix):  # No database work for a guess.
            raise AuthenticationFailed("Invalid token.")
        digest = token_digest(raw)
        entry = await stored_credential(TOKEN_SCHEME, digest, config.PRINCIPAL_CACHE)
        token = None if entry is None else entry.credential
        if entry is None or token is None or token.kind != TokenKind.ACCESS:
            raise AuthenticationFailed("Invalid token.")
        if token.expired():
            forget(entry)
            raise AuthenticationFailed("Token expired.")
        every = LAST_USED_EVERY
        if config.IDLE_TIMEOUT is not None:
            every = last_used_every()
            entry = await unless_idle(
                entry, TOKEN_SCHEME, digest, config.PRINCIPAL_CACHE
            )
            if entry is None:
                raise AuthenticationFailed(IDLE)
        user = entry.user()
        if user is None:
            raise AuthenticationFailed("User inactive or deleted.")
        await accept(entry, config.PRINCIPAL_CACHE, every=every)
        return Credentials(
            user, scheme=TOKEN_SCHEME, token_id=token.id, scopes=token.scopes
        )

    async def issue(self, uow: AuthUnitOfWork, user: AbstractBaseUser) -> IssuedTokens:
        """One access token for ``AUTH.TOKEN_TTL``."""
        ttl = auth_settings().TOKEN_TTL
        raw = await uow.tokens.issue(user.id, ttl=ttl, name="login")
        return IssuedTokens(access_token=raw, expires_in=_seconds(ttl))


class JWTAuthentication(BaseAuthentication):
    """Signed access tokens (JSON Web Tokens) and opaque refresh tokens.

    An access token works until it expires (``AUTH.JWT.ACCESS_TOKEN_TTL``,
    5 minutes by default), and only while its login lasts: logging out, or
    a refresh token used twice, ends it at once. A refresh token buys a new
    pair, once. Other services can check access tokens with the public key
    alone. Needs PyJWT: ``pip install "domainkit[jwt]"``.

    Each token names its key in its header (``kid``): the signing key, or
    one of ``AUTH.JWT.PREVIOUS_KEYS`` for a token lifetime after it
    retired, so a key is replaced without refusing the tokens it signed
    (ADR 0097).
    """

    def __init__(self) -> None:
        """Load PyJWT and check the keys; the settings come from ``AUTH.JWT``.

        Raises:
            ImproperlyConfigured: A key is missing, short, or not a key.
            CheckFailed: The previous keys are unsafe (``auth.E008``,
                ``auth.E009``), every problem listed.
        """
        try:
            import jwt
            from jwt.algorithms import get_default_algorithms
        except ImportError as exc:
            raise ImproperlyConfigured(
                'JWTAuthentication needs PyJWT: pip install "domainkit[jwt]".'
            ) from exc
        self.jwt = jwt
        self.config = auth_settings().JWT
        self.signing_key, self.verifying_key = _keys(self.config)
        self._check_keys()
        self.key_id, self.keys = _key_set(
            self.config, self.verifying_key, get_default_algorithms()
        )
        """The signing key's id; every key that verifies, by id."""

    async def authenticate(self, connection: HTTPConnection) -> Credentials | None:
        """The user of a valid access token whose login has not ended.

        The login and the user are kept as opaque tokens' are, by login
        and user; the signature and the times are checked every time.
        """
        raw = bearer_token(connection)
        if raw is None or raw.count(".") != 2:  # Not a JWT: header.payload.signature.
            return None
        claims = self.decode(raw)
        session = _uuid(claims["sid"])
        config = auth_settings().PRINCIPAL_CACHE
        entry = await login_principal(session, _uuid(claims["sub"]), config)
        if entry.login is None or not entry.login.active_at(utc_now()):
            raise AuthenticationFailed("Session ended; log in again.")
        user = entry.user()
        if user is None:
            raise AuthenticationFailed("User inactive or deleted.")
        remember(entry, config)
        return Credentials(user, scheme=JWT_SCHEME, session=session)

    async def issue(self, uow: AuthUnitOfWork, user: AbstractBaseUser) -> IssuedTokens:
        """An access token and a refresh token that starts a new family."""
        family = uuid4()
        refresh = await uow.tokens.issue(
            user.id,
            kind=TokenKind.REFRESH,
            ttl=self.config.REFRESH_TOKEN_TTL,
            name="login",
            family=family,
        )
        return self._pair(user, family, refresh)

    async def refresh(self, uow: AuthUnitOfWork, refresh_token: str) -> IssuedTokens:
        """Exchange a refresh token, once, for a new pair.

        The new refresh token expires with the login it descends from. A
        used refresh token that comes back means someone else may hold one
        of the family: the whole family is revoked.
        """
        token = await uow.tokens.find(refresh_token)
        if (
            token is None
            or token.kind != TokenKind.REFRESH
            or token.family is None
            or token.expires_at is None
            or token.expired()
        ):
            raise AuthenticationFailed("Invalid refresh token.")
        if is_login(token.kind, token.name) and idle_since(token.created_at, utc_now()):
            raise AuthenticationFailed(IDLE)
        if token.used_at is not None or not await uow.tokens.mark_used(token):
            await uow.tokens.revoke_family(token.family)
            security_logger.warning(
                "A refresh token came back after use; its login %s is ended",
                token.family,
            )
            if (user := await uow.users.find(UserId(token.user_id))) is not None:
                user.record(RefreshTokenReused(user_id=user.id, family=token.family))
            await uow.commit()
            raise AuthenticationFailed("Refresh token reused; log in again.")
        user = await active_user(uow, token.user_id)
        now = utc_now()
        refresh = await uow.tokens.issue(
            user.id,
            kind=TokenKind.REFRESH,
            ttl=token.expires_at - now,
            name=token.name,
            family=token.family,
            now=now,
        )
        return self._pair(user, token.family, refresh)

    def encode(self, user: AbstractBaseUser, session: UUID) -> str:
        """A signed access token for ``user`` in the login ``session``."""
        now = utc_now()
        claims: dict[str, Any] = {
            "sub": str(user.id),
            "iat": now,
            "exp": now + self.config.ACCESS_TOKEN_TTL,
            "jti": uuid4().hex,
            "sid": str(session),
        }
        if self.config.ISSUER is not None:
            claims["iss"] = self.config.ISSUER
        if self.config.AUDIENCE is not None:
            claims["aud"] = self.config.AUDIENCE
        token: str = self.jwt.encode(
            claims,
            self.signing_key,
            algorithm=self.config.ALGORITHM,
            headers={"kid": self.key_id},
        )
        return token

    def decode(self, raw: str) -> dict[str, Any]:
        """The claims of a token signed with one of our keys, current, and for us.

        The header's ``kid`` picks the key, and with it the one algorithm
        checked; a token without one (issued before 0.11) is checked with
        the signing key alone.

        Raises:
            AuthenticationFailed: Anything else.
        """
        key = self._key_of(raw)
        required = ["exp", "iat", "sub", "sid"]
        required += [
            claim
            for claim, value in (
                ("iss", self.config.ISSUER),
                ("aud", self.config.AUDIENCE),
            )
            if value is not None
        ]
        try:
            claims: dict[str, Any] = self.jwt.decode(
                raw,
                key.key,
                algorithms=[key.algorithm],
                audience=self.config.AUDIENCE,
                issuer=self.config.ISSUER,
                # The times are checked below, on domainkit's clock.
                options={
                    "require": required,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except self.jwt.InvalidTokenError:
            raise AuthenticationFailed("Invalid token.") from None
        _check_times(claims, self.config.LEEWAY)
        return claims

    def _key_of(self, raw: str) -> "_VerifyingKey":
        """The key the token's ``kid`` names, while it verifies.

        Never every key in turn: an id the set lacks is refused.

        Raises:
            AuthenticationFailed: The header does not parse, names no key
                held, or a key past its retirement.
        """
        named = _named_key(raw, self.key_id)
        key = self.keys.get(named) if isinstance(named, str) else None
        if key is None or (key.until is not None and utc_now() >= key.until):
            raise AuthenticationFailed("Invalid token.")
        return key

    def _pair(
        self, user: AbstractBaseUser, session: UUID, refresh: str
    ) -> IssuedTokens:
        """An access token for ``user`` with its refresh token."""
        return IssuedTokens(
            access_token=self.encode(user, session),
            expires_in=_seconds(self.config.ACCESS_TOKEN_TTL),
            refresh_token=refresh,
        )

    def _check_keys(self) -> None:
        """Sign and verify a token once, so bad keys fail at startup."""
        try:
            probe = self.jwt.encode(
                {}, self.signing_key, algorithm=self.config.ALGORITHM
            )
            self.jwt.decode(
                probe, self.verifying_key, algorithms=[self.config.ALGORITHM]
            )
        except (self.jwt.PyJWTError, ValueError, TypeError) as exc:
            raise ImproperlyConfigured(
                f"AUTH.JWT: the keys do not sign and verify {self.config.ALGORITHM} "
                f"tokens ({type(exc).__name__})."
            ) from None


@cache
def load_authenticators(config: AuthSettings) -> tuple[BaseAuthentication, ...]:
    """Instantiate ``config.AUTHENTICATION_CLASSES`` (once per settings)."""
    authenticators = []
    for path in config.AUTHENTICATION_CLASSES:
        try:
            cls = import_string(path)
        except ImportError as exc:
            raise ImproperlyConfigured(f"AUTH.AUTHENTICATION_CLASSES: {exc}") from exc
        if not (isinstance(cls, type) and issubclass(cls, BaseAuthentication)):
            raise ImproperlyConfigured(
                f"AUTH.AUTHENTICATION_CLASSES: {path!r} is not a BaseAuthentication."
            )
        authenticators.append(cls())
    return tuple(authenticators)


def get_authenticators() -> tuple[BaseAuthentication, ...]:
    """The configured authentication classes, in order."""
    return load_authenticators(auth_settings())


def bearer_token(connection: HTTPConnection) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header, if any."""
    scheme, _, token = connection.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


async def stored_credential(
    scheme: str, digest: str, config: PrincipalCacheSettings
) -> Entry | None:
    """Token ``digest`` and the user it names: kept, or read in two queries.

    None: no token has the digest. The entry of a token just read is not
    kept yet: ``accept`` keeps it once the class accepts the credential.
    Requests of one credential that miss at once read it once
    (``kept_or_read``). The stamps (``STAMPS``) of ``ALL`` and the token
    are read before the unit of work opens; the others (and any absent one)
    after the token, which names the user, and before the user, whose read
    checks that the token is still stored.
    """
    key = (scheme, digest)

    async def read(started: int) -> Entry | None:
        ahead = await stamps_ahead(config, [token_target(digest)])
        async with AuthUnitOfWork() as uow:
            credential = await uow.principals.credential(digest)
            if credential is None:
                return None
            targets = [user_target(credential.user_id), token_target(digest)]
            if credential.family is not None:
                targets.append(family_target(credential.family))
            entry = Entry(
                key,
                None,
                credential=credential,
                targets=tuple(targets),
                started=started,
                last_used=credential.last_used_at,
            )
            await stamps_before_reading(entry, config, ahead)
            snapshot = await uow.principals.principal(credential.user_id, token=digest)
        return entry.holding(snapshot)

    return await kept_or_read(key, config, read)


async def unless_idle(
    entry: Entry, scheme: str, digest: str, config: PrincipalCacheSettings
) -> Entry | None:
    """``entry``, unless its login idled past ``AUTH.IDLE_TIMEOUT``; then None.

    A kept entry that looks idle is read again first: another process may
    have served the login since, and written its last use.
    """
    credential = entry.credential
    if credential is None or not credential.idle(entry.last_used):
        return entry
    forget(entry)
    fresh = await stored_credential(scheme, digest, config)
    found = None if fresh is None else fresh.credential
    if fresh is None or found is None or found.idle(fresh.last_used):
        return None
    return fresh


async def accept(
    entry: Entry,
    config: PrincipalCacheSettings,
    *,
    touch: bool = True,
    every: timedelta = LAST_USED_EVERY,
) -> None:
    """Keep ``entry``, which its class accepted; record that its token was used.

    ``last_used_at`` is written when due (``every``: ``last_used_every()``),
    by one of the requests that find it due, in a transaction of its own;
    kept or not, no other request opens one.
    """
    if touch and entry.credential is not None:
        now = utc_now()
        if principal_cache.claim_touch(entry, now, every):
            async with AuthUnitOfWork() as uow:
                if await uow.tokens.touch_digest(entry.credential.id, now):
                    await uow.commit()
    remember(entry, config)


async def login_principal(
    session: UUID, user_id: UUID, config: PrincipalCacheSettings
) -> Entry:
    """A JSON Web Token's login and user: kept, or read in two queries.

    A kept login past its last token's expiry is read again: a refresh may
    have given it a newer one.
    """
    key = (JWT_SCHEME, f"{session}:{user_id}")

    async def read(started: int) -> Entry:
        entry = Entry(
            key,
            None,
            targets=(user_target(user_id), family_target(session)),
            started=started,
        )
        await stamps_before_reading(entry, config)
        async with AuthUnitOfWork() as uow:
            entry.login = await uow.principals.login(session)
            if not entry.login.active_at(utc_now()):
                return entry
            snapshot = await uow.principals.principal(user_id)
        return entry.holding(snapshot)

    return cast(Entry, await kept_or_read(key, config, read, _login_active))


def _login_active(entry: Entry) -> bool:
    """Whether a kept login still has an unexpired token."""
    return entry.login is not None and entry.login.active_at(utc_now())


async def active_user(uow: AuthUnitOfWork, user_id: UUID) -> AbstractBaseUser:
    """The active user ``user_id``, loaded with groups and permissions.

    Raises:
        AuthenticationFailed: The user is gone or inactive.
    """
    user = await uow.users.find(UserId(user_id))
    if user is None or not user.is_active:
        raise AuthenticationFailed("User inactive or deleted.")
    return user


def _keys(config: JWTSettings) -> tuple[bytes | str, bytes | str]:
    """The signing and verifying keys, checked for their algorithm."""
    if config.SIGNING_KEY is None:
        raise ImproperlyConfigured("AUTH.JWT.SIGNING_KEY is required.")
    secret = config.SIGNING_KEY.get_secret_value()
    if config.ALGORITHM in HMAC_ALGORITHMS:
        key = secret.encode()
        # RFC 7518 3.2: a key at least as long as the hash.
        if len(key) < (needed := int(config.ALGORITHM[2:]) // 8):
            raise ImproperlyConfigured(
                f"AUTH.JWT.SIGNING_KEY must have at least {needed} bytes for "
                f"{config.ALGORITHM}."
            )
        return key, key
    if config.VERIFYING_KEY is None:
        raise ImproperlyConfigured(
            f"AUTH.JWT.VERIFYING_KEY (the public key) is required for "
            f"{config.ALGORITHM}."
        )
    return secret, config.VERIFYING_KEY


@dataclass(frozen=True, slots=True)
class _VerifyingKey:
    """A key that verifies the tokens naming its id, with its algorithm only."""

    algorithm: str
    key: Any = field(repr=False)
    """The HMAC secret's bytes, or the public key, loaded once."""
    until: datetime | None = None
    """When the tokens it signed stop being accepted; None: the signing key's."""


def _named_key(raw: str, default: str) -> object:
    """The ``kid`` of a token's header, unverified, or ``default`` without one.

    Read here rather than by PyJWT, which decodes the whole token to read
    its header: it only picks a key, and PyJWT then checks that key signed
    this very header. None: the header does not parse.
    """
    head = raw.partition(".")[0]
    try:
        header = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    except (ValueError, RecursionError):  # binascii and Unicode errors are too.
        return None
    return header.get("kid", default) if isinstance(header, dict) else None


def _key_set(
    config: JWTSettings, verifying_key: bytes | str, algorithms: dict[str, Any]
) -> tuple[str, dict[str, _VerifyingKey]]:
    """The signing key's id, and every key that verifies, by id.

    A previous key verifies until it retired plus ``ACCESS_TOKEN_TTL`` and
    ``LEEWAY``: nothing it signed before retiring outlives that.

    Raises:
        ImproperlyConfigured: A previous key is short, or not a key.
        CheckFailed: A previous key is the signing key or listed twice
            (``auth.E008``), or retires more than a day ahead (``auth.E009``).
    """
    current = algorithms[config.ALGORITHM].prepare_key(verifying_key)
    key_id = _key_id(config.ALGORITHM, current, algorithms)
    keys = {key_id: _VerifyingKey(config.ALGORITHM, current)}
    named: dict[str, str] = {}  # Where each previous key is listed, by id.
    grace = config.ACCESS_TOKEN_TTL + config.LEEWAY
    latest = utc_now() + MAX_RETIREMENT_AHEAD
    problems = []
    for index, previous in enumerate(config.PREVIOUS_KEYS):
        where = f"AUTH.JWT.PREVIOUS_KEYS[{index}]"
        algorithm = previous.ALGORITHM or config.ALGORITHM
        key = _previous_key(
            where, algorithm, previous.KEY.get_secret_value(), algorithms
        )
        found = _key_id(algorithm, key, algorithms)
        if found == key_id:
            problems.append(
                CheckMessage(
                    "auth.E008",
                    f"{where} is the signing key: a key that signs is not "
                    "retired; give the old key, or none.",
                )
            )
        elif found in named:
            problems.append(
                CheckMessage(
                    "auth.E008",
                    f"{where} is the key of {named[found]}; list each key once.",
                )
            )
        if latest < previous.RETIRED_AT:
            problems.append(
                CheckMessage(
                    "auth.E009",
                    f"{where}.RETIRED_AT is {previous.RETIRED_AT}, more than a day "
                    "ahead: a previous key verifies only what it signed until it "
                    "retired, plus ACCESS_TOKEN_TTL and LEEWAY. Set the time its "
                    "rollout ends.",
                )
            )
        named.setdefault(found, where)
        keys.setdefault(
            found, _VerifyingKey(algorithm, key, previous.RETIRED_AT + grace)
        )
    if problems:
        raise CheckFailed("AUTH settings cannot be used", problems)
    return key_id, keys


def _previous_key(
    where: str, algorithm: str, raw: str, algorithms: dict[str, Any]
) -> Any:  # noqa: ANN401 - a key PyJWT loaded.
    """A previous key, loaded for ``algorithm``: a long enough secret, or a public key.

    Raises:
        ImproperlyConfigured: It is short, not a key, or a private key.
    """
    from jwt import PyJWTError

    hashed = algorithm in HMAC_ALGORITHMS
    if hashed and len(raw.encode()) < (needed := int(algorithm[2:]) // 8):
        raise ImproperlyConfigured(
            f"{where}.KEY must have at least {needed} bytes for {algorithm}."
        )
    try:
        key = algorithms[algorithm].prepare_key(raw)
    except (PyJWTError, ValueError, TypeError) as exc:
        kind = "secret" if hashed else "public key"
        raise ImproperlyConfigured(
            f"{where}.KEY is not a {kind} for {algorithm} ({type(exc).__name__})."
        ) from None
    if not hashed and "d" in algorithms[algorithm].to_jwk(key, as_dict=True):
        raise ImproperlyConfigured(
            f"{where}.KEY is a private key: give its public key in PEM; a retired "
            "key only verifies."
        )
    return key


def _key_id(algorithm: str, key: Any, algorithms: dict[str, Any]) -> str:  # noqa: ANN401
    """The id a token names ``key`` by: it never shows the key.

    An HMAC secret's is a keyed hash: HMAC-SHA256 of ``KEY_ID_LABEL`` under
    the secret, its first 16 bytes. A public key's is its JWK thumbprint
    (RFC 7638, SHA-256), which any service holding it can compute. Both in
    base64url, without padding.
    """
    if algorithm in HMAC_ALGORITHMS:
        digest = hmac.new(key, KEY_ID_LABEL, hashlib.sha256).digest()[:KEY_ID_BYTES]
        return _base64url(digest)
    jwk = algorithms[algorithm].to_jwk(key, as_dict=True)
    members = {name: jwk[name] for name in THUMBPRINT_MEMBERS[jwk["kty"]]}
    canonical = json.dumps(members, separators=(",", ":"))  # Sorted already.
    return _base64url(hashlib.sha256(canonical.encode()).digest())


def _base64url(data: bytes) -> str:
    """``data`` in base64url, without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _check_times(claims: dict[str, Any], leeway: timedelta) -> None:
    """Check ``iat``, ``nbf``, and ``exp`` on domainkit's clock, as PyJWT would.

    PyJWT reads the host's clock, which a frozen clock (``domainkit.clock``)
    does not move; the tokens are issued on domainkit's.

    Raises:
        AuthenticationFailed: The token is malformed, not valid yet, or expired.
    """
    now = utc_now().timestamp()
    slack = leeway.total_seconds()
    try:
        issued, expires = int(claims["iat"]), int(claims["exp"])
        starts = int(claims.get("nbf", issued))
    except (TypeError, ValueError, OverflowError):
        raise AuthenticationFailed("Invalid token.") from None
    if max(issued, starts) > now + slack:
        raise AuthenticationFailed("Invalid token.")
    if expires <= now - slack:
        raise AuthenticationFailed("Token expired.")


def _uuid(value: object) -> UUID:
    """A claim as a UUID.

    Raises:
        AuthenticationFailed: It is not one.
    """
    try:
        return UUID(str(value))
    except ValueError:
        raise AuthenticationFailed("Invalid token.") from None


def _seconds(ttl: timedelta | None) -> int | None:
    """Whole seconds of ``ttl``; None stays None."""
    return None if ttl is None else int(ttl.total_seconds())
