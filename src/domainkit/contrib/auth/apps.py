"""Install ``domainkit.contrib.auth`` (after ``domainkit.contrib.contenttypes``)."""

import dataclasses
from collections.abc import Sequence
from importlib import import_module
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.apps import DomainConfig, registry
from domainkit.checks import CheckFailed, CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS, REDIS_SCHEMES
from domainkit.contrib.contenttypes.sync import has_tables
from domainkit.db import SqlAlchemyUnitOfWork, metadata
from domainkit.db.hooks import on_committed, on_migrated, on_stage
from domainkit.db.routing import router
from domainkit.ddd import DomainEvent, event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import routes_need_users, served_routes
from domainkit.http.ratelimit import MemoryBackend
from domainkit.utils.module_loading import import_string

from .authentication import get_authenticators
from .backends import get_backends
from .hashers import insecure_hashers
from .models import AbstractBaseUser, AbstractUser, User
from .permissions import declared_permissions, sync_permissions
from .principals import invalidate_all, stamp_committed, stamp_pending, watch
from .settings import PRINCIPAL_TTL, AuthSettings, auth_settings
from .tables import DEFAULT_USER_TABLES, permissions, start_mappers, users
from .validators import get_password_validators

CONTENTTYPES = "domainkit.contrib.contenttypes"
MESSAGING = "domainkit.contrib.messaging"
MAIL = "domainkit.contrib.mail"
PRIVACY = "domainkit.contrib.privacy"
MAILING_ROUTERS = ("recovery", "signup")
"""Modules whose routes send mail, and need a user model with an address."""
COOKIE_AUTHENTICATION = "domainkit.contrib.auth.cookies.CookieAuthentication"
AUTHENTICATION_MIDDLEWARE = "domainkit.contrib.auth.middleware.AuthenticationMiddleware"
OUTSIDE_AUTHENTICATION = ("domainkit.http.middleware.RequestContextMiddleware",)
"""Middleware that must run before ``AuthenticationMiddleware``."""
INSIDE_AUTHENTICATION = (
    "domainkit.http.ratelimit.RateLimitMiddleware",
    "domainkit.contrib.idempotency.middleware.IdempotencyMiddleware",
    "domainkit.http.websockets.WebSocketLimitsMiddleware",
)
"""Middleware that must run after it, to count users rather than addresses."""
REDIS_BACKEND = "domainkit.http.ratelimit.RedisBackend"
"""The rate-limit backend every process shares."""
RELATIONSHIPS = frozenset({"groups", "user_permissions"})
"""User fields stored in link tables, not in the user's own table."""


class AuthConfig(DomainConfig):
    """Users, groups, and permissions; the user model may be swapped."""

    label = "auth"
    verbose_name = "Authentication and authorization"
    settings_class = AuthSettings
    requires = (CONTENTTYPES,)

    user_model: type[AbstractBaseUser] = User
    """The user class in use (``AUTH.USER_MODEL``)."""
    user_table: Table = users
    """Its table (``AUTH.USER_TABLE``, or the default one)."""

    @property
    def database(self) -> str:
        """The one alias holding users, groups, and permissions."""
        return router.require_standalone(
            router.target_for(self.label), "DATABASE_ROUTING['auth']"
        )

    @property
    def swapped(self) -> bool:
        """Whether a custom user table replaces the default one."""
        return self.user_table is not users

    def ready(self) -> None:
        """Load the user model, check the configuration, and map the tables."""
        config = auth_settings()
        self.user_model = _user_model(config.USER_MODEL)
        self.user_table = _user_table(config, self.user_model)
        self._check_routing()
        self._check_deployment(config)
        _check_middleware()
        get_backends()
        get_authenticators()
        get_password_validators()
        start_mappers(self.user_model, self.user_table)
        watch(self.user_model)
        on_stage(self.stamp_changes)
        on_committed(self.wait_for_stamps)
        on_migrated(self.create_permissions)
        invalidate_all()  # Settings, models, or databases may have changed.
        declared_permissions()
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")
            for module in MAILING_ROUTERS:
                import_module(f"{self.name}.{module}").register_tasks()
        if registry.is_installed(PRIVACY):
            from domainkit.contrib.privacy import PersonalDataErased

            from .erasure import forget_credentials

            event_bus.subscribe(PersonalDataErased, forget_credentials)

    async def stamp_changes(
        self,
        uow: SqlAlchemyUnitOfWork,
        events: Sequence[DomainEvent],
        tasks: Sequence[Any],
    ) -> None:
        """Stamp what the commit will change in principals, before it too."""
        await stamp_pending(uow.session, auth_settings().PRINCIPAL_CACHE)

    async def wait_for_stamps(self, uow: SqlAlchemyUnitOfWork) -> None:
        """Wait for the stamps of what the commit changed, for other processes."""
        await stamp_committed(uow.session)

    async def issue_test_token(
        self, permissions: Sequence[str], fields: dict[str, Any]
    ) -> str:
        """Store a new user granted ``permissions``; its access token (``testing``).

        ``fields`` set the user's fields; the username is unique unless
        given. The token comes from the first ``AUTH.AUTHENTICATION_CLASSES``
        class, as a login's would.
        """
        from .uow import AuthUnitOfWork

        username = fields.pop(self.user_model.USERNAME_FIELD, f"user-{uuid4().hex}")
        async with AuthUnitOfWork() as uow:
            user = await uow.users.create_user(username, **fields)
            user.grant(*await uow.permissions.get_many(permissions))
            issued = await get_authenticators()[0].issue(uow, user)
            await uow.commit()
        return issued.access_token

    async def issue_test_token_for(self, user_id: Any) -> str:  # noqa: ANN401 - any user model's id.
        """An access token for the stored user ``user_id`` (``testing``).

        From the first ``AUTH.AUTHENTICATION_CLASSES`` class, as a login's
        would be: a user made by a factory or by ``create_user``.

        Raises:
            NotFound: No user has the id.
        """
        from .uow import AuthUnitOfWork

        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(user_id)
            issued = await get_authenticators()[0].issue(uow, user)
            await uow.commit()
        return issued.access_token

    def check_application(self, app: FastAPI) -> list[str]:
        """Rules no middleware can answer, and what auth's own routes lack.

        Routes that declare no rule are the core's to find
        (``REQUIRE_ROUTE_RULES``), with or without this app. In staging and
        production, attempts counted in each process too (``auth.E105``),
        unless ``AUTH.REQUIRE_SHARED_THROTTLES`` is False: the application
        refuses to start, and ``check`` fails; workers and commands start.
        """
        problems: list[str] = []
        if AUTHENTICATION_MIDDLEWARE not in settings.MIDDLEWARE and routes_need_users(
            app
        ):
            problems.append(
                CheckMessage(
                    "auth.E101",
                    "Routes check users, but MIDDLEWARE lacks "
                    f"{AUTHENTICATION_MIDDLEWARE}.",
                )
            )
        problems.extend(self._mailing_problems(app))
        problems.extend(self._unshared_throttles(required=True))
        if (
            self._mounted(app, "cookies")
            and COOKIE_AUTHENTICATION not in auth_settings().AUTHENTICATION_CLASSES
        ):
            problems.append(
                CheckMessage(
                    "auth.E102",
                    f"Routes of {self.name}.cookies start browser sessions, but "
                    f"AUTH.AUTHENTICATION_CLASSES lacks {COOKIE_AUTHENTICATION}.",
                )
            )
        return problems

    def _unshared_throttles(self, *, required: bool) -> list[CheckMessage]:
        """Attempts counted in each process, in staging and production.

        Refused (``auth.E105``) while ``AUTH.REQUIRE_SHARED_THROTTLES``
        holds, the default since 0.11 (``required``); warned (``auth.W004``)
        with it False, as 0.10 did (AUTH-09).
        """
        config = auth_settings()
        if (
            settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS
            or config.REQUIRE_SHARED_THROTTLES is not required
            or not _counted_per_process(config)
        ):
            return []
        message = (
            f"RATE_LIMIT_BACKEND {settings.RATE_LIMIT_BACKEND} counts in each "
            "process, so with several processes or replicas each allows "
            "AUTH.LOGIN_RATE, AUTH.MFA.RATE, and the other attempt rates on its "
            "own: passwords and codes can be guessed that many times faster."
        )
        if required:
            return [
                CheckMessage(
                    "auth.E105",
                    message,
                    f"Set RATE_LIMIT_BACKEND to {REDIS_BACKEND}, shared by every "
                    "process; with one process only, set "
                    "AUTH.REQUIRE_SHARED_THROTTLES = False to only warn "
                    "(auth.W004), as 0.10 did.",
                )
            ]
        return [
            CheckMessage(
                "auth.W004",
                f"{message} Set RATE_LIMIT_BACKEND to {REDIS_BACKEND}, shared by "
                "every process.",
            )
        ]

    def _mounted(self, app: FastAPI, name: str) -> bool:
        """Whether a route of the router in module ``name`` is served by ``app``."""
        called = {
            dependant.call for _, dependant in served_routes(app.routes) if dependant
        }
        module = import_module(f"{self.name}.{name}")
        return bool(called & {route.endpoint for route in module.router.routes})

    def _mailing_problems(self, app: FastAPI) -> list[str]:
        """What routes that send mail lack: the apps, or an address to send to."""
        problems: list[str] = []
        for name in MAILING_ROUTERS:
            if not self._mounted(app, name):
                continue
            module = f"{self.name}.{name}"
            installed = registry.is_installed
            if missing := [name for name in (MESSAGING, MAIL) if not installed(name)]:
                problems.append(
                    CheckMessage(
                        "auth.E103",
                        f"Routes of {module} send mail: add {', '.join(missing)} "
                        "to INSTALLED_DOMAINS.",
                    )
                )
            if self.user_model.EMAIL_FIELD is None or not hasattr(
                self.user_model, "verify_email"
            ):
                problems.append(
                    CheckMessage(
                        "auth.E104",
                        f"Routes of {module} need a user model with an "
                        "EMAIL_FIELD and email_verified, as AbstractUser has.",
                    )
                )
        return problems

    def deployment_warnings(self) -> list[str]:
        """Weak choices in staging and production.

        Login and code checks without a rate, or counted in each process
        (with ``AUTH.REQUIRE_SHARED_THROTTLES = False``; refused otherwise),
        cookies without ``__Host-`` where it could hold, and principals
        kept longer than ``PRINCIPAL_TTL`` where nothing tells other
        processes of changes.
        """
        if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
            return []
        config = auth_settings()
        cookie = config.COOKIE
        warnings: list[str] = []
        principals = config.PRINCIPAL_CACHE
        if (
            principals.ENABLED
            and principals.STAMPS is None
            and principals.TTL > PRINCIPAL_TTL
        ):
            seconds = principals.TTL.total_seconds()
            warnings.append(
                CheckMessage(
                    "auth.W001",
                    f"AUTH.PRINCIPAL_CACHE.TTL is {seconds:g} seconds without "
                    "STAMPS: with several processes, a revoked token, a "
                    "deactivated user, or a removed permission keeps working in "
                    f"the others for up to {seconds:g} seconds. Set STAMPS to a "
                    "Redis cache, or the TTL to "
                    f"{PRINCIPAL_TTL.total_seconds():g} seconds or less.",
                )
            )
        if config.LOGIN_RATE is None:
            warnings.append(
                CheckMessage(
                    "auth.W002",
                    "AUTH.LOGIN_RATE is None, so passwords can be guessed without "
                    'limit; set it, such as "10/minute".',
                )
            )
        if config.MFA.RATE is None:
            warnings.append(
                CheckMessage(
                    "auth.W003",
                    "AUTH.MFA.RATE is None, so one-time codes can be guessed "
                    'without limit; set it, such as "10/hour".',
                )
            )
        warnings.extend(self._unshared_throttles(required=False))
        if (
            COOKIE_AUTHENTICATION in config.AUTHENTICATION_CLASSES
            and cookie.DOMAIN is None
            and cookie.PATH == "/"
        ):
            warnings.extend(
                CheckMessage(
                    "auth.W005",
                    f"AUTH.COOKIE.{key} {name!r} lacks the __Host- prefix, so "
                    "another host of the site (a subdomain) can set it in "
                    f"browsers; name it __Host-{name}, or leave {key} out.",
                )
                for key, name in (
                    ("NAME", cookie.NAME),
                    ("CSRF_NAME", cookie.CSRF_NAME),
                )
                if name is not None and not name.startswith("__Host-")
            )
        return warnings

    def holds_table(self, table: Table) -> bool:
        """The default user's tables, unless a custom user replaces them."""
        return not (self.swapped and table in DEFAULT_USER_TABLES)

    async def create_permissions(
        self, connection: AsyncConnection, database: str
    ) -> None:
        """Create the permissions models declare, after ``migrate``."""
        # The permissions table's foreign key needs the content types one.
        if database == self.database and await has_tables(connection, permissions):
            await sync_permissions(connection)

    def _check_routing(self) -> None:
        """Users, their links, and content types must share one target.

        ``ContentTypesConfig.ready`` checks the target is not a shard group.
        """
        targets = {
            "auth": router.target_for(self.label),
            "contenttypes": router.target_for("contenttypes"),
            self.user_table.name: router.target_for(self.user_table.info["domain"]),
        }
        if len(set(targets.values())) > 1:
            where = ", ".join(
                f"{name} in {target!r}" for name, target in targets.items()
            )
            raise ImproperlyConfigured(
                f"Users, groups, permissions, and content types need one database "
                f"(foreign keys join them): {where}."
            )

    def _check_deployment(self, config: AuthSettings) -> None:
        """Refuse settings that cannot hold anywhere, and unsafe ones where deployed.

        Staging and production refuse hashers made for tests, plain
        cookies, certificate proxies given as networks, and links sent by
        mail over plain HTTP. Every problem is reported at once, each with
        its id (``domainkit.checks``).

        Raises:
            CheckFailed: Any of them.
        """
        if errors := self._deployment_errors(config):
            raise CheckFailed("AUTH settings cannot be used", errors)

    def _deployment_errors(self, config: AuthSettings) -> list[CheckMessage]:
        """What ``_check_deployment`` refuses, in the order it is checked."""
        errors = []
        if config.CLIENT_CERT.HEADER is not None and not config.CLIENT_CERT.PROXIES:
            errors.append(
                CheckMessage(
                    "auth.E001",
                    "AUTH.CLIENT_CERT.HEADER is believed only from "
                    "AUTH.CLIENT_CERT.PROXIES; list the address of the proxy that "
                    "sets it.",
                )
            )
        if (
            config.CLIENT_CERT.HEADER is not None
            and config.CLIENT_CERT.MATCH == "subject"
            and config.CLIENT_CERT.VERIFY_HEADER is None
        ):
            errors.append(
                CheckMessage(
                    "auth.E002",
                    'AUTH.CLIENT_CERT: MATCH = "subject" through HEADER needs '
                    "VERIFY_HEADER, or anyone could sign a certificate naming any "
                    "user.",
                )
            )
        stamps = config.PRINCIPAL_CACHE.STAMPS
        if stamps is not None and (
            stamps not in settings.CACHES
            or settings.CACHES[stamps].scheme not in REDIS_SCHEMES
        ):
            errors.append(
                CheckMessage(
                    "auth.E003",
                    f"AUTH.PRINCIPAL_CACHE.STAMPS must name a Redis cache of CACHES "
                    f"that every process shares, not {stamps!r}.",
                )
            )
        if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
            errors.extend(self._deployed_errors(config))
        return errors

    def _deployed_errors(self, config: AuthSettings) -> list[CheckMessage]:
        """What staging and production refuse besides."""
        where = settings.ENVIRONMENT
        errors = []
        if insecure := insecure_hashers(config.PASSWORD_HASHERS):
            errors.append(
                CheckMessage(
                    "auth.E004",
                    f"AUTH.PASSWORD_HASHERS {insecure} are for tests; remove them "
                    f"in {where}.",
                )
            )
        if config.COOKIE.SECURE is False:
            errors.append(
                CheckMessage(
                    "auth.E005",
                    f"AUTH.COOKIE.SECURE = False sends session cookies over plain "
                    f"HTTP; remove it in {where}.",
                )
            )
        links = {
            "PASSWORD_RESET_URL": config.PASSWORD_RESET_URL,
            "EMAIL_VERIFICATION_URL": config.EMAIL_VERIFICATION_URL,
            "SIGNUP_URL": config.SIGNUP_URL,
        }
        if plain := [
            key
            for key, url in links.items()
            if url is not None and url.lower().startswith("http://")
        ]:
            errors.append(
                CheckMessage(
                    "auth.E006",
                    f"AUTH.{', AUTH.'.join(plain)} send links with their tokens "
                    f"over plain HTTP; use https:// in {where}.",
                )
            )
        if networks := [
            str(network)
            for network in config.CLIENT_CERT.PROXIES
            if network.num_addresses > 1
        ]:
            errors.append(
                CheckMessage(
                    "auth.E007",
                    f"AUTH.CLIENT_CERT.PROXIES must list single addresses (/32 or "
                    f"/128) in {where}, not networks "
                    f"({', '.join(networks)}): any host there could name any user.",
                )
            )
        return errors


def _check_middleware() -> None:
    """``AuthenticationMiddleware`` sets the actor others read: order matters.

    It binds the actor to the context ``RequestContextMiddleware`` makes, so
    that one must be listed, and first: without it no actor is bound, and
    what reads one (the audit trail, rate limits, idempotency keys,
    ``@cache_page``) cannot tell users apart.
    """
    order = list(settings.MIDDLEWARE)
    if AUTHENTICATION_MIDDLEWARE not in order:
        return
    position = order.index(AUTHENTICATION_MIDDLEWARE)
    for path in OUTSIDE_AUTHENTICATION:
        if path not in order or order.index(path) > position:
            raise ImproperlyConfigured(
                f"MIDDLEWARE: list {path} before {AUTHENTICATION_MIDDLEWARE}."
            )
    for path in INSIDE_AUTHENTICATION:
        if path in order and order.index(path) < position:
            raise ImproperlyConfigured(
                f"MIDDLEWARE: list {AUTHENTICATION_MIDDLEWARE} before {path}, so it "
                "counts users rather than client addresses."
            )


def _counted_per_process(config: AuthSettings) -> bool:
    """Whether attempts are throttled, in a backend each process keeps alone."""
    rates = (
        config.LOGIN_RATE,
        config.EMAIL_RATE,
        config.MFA.RATE,
        config.MFA.ADDRESS_RATE,
    )
    if all(rate is None for rate in rates):
        return False
    try:
        backend = import_string(settings.RATE_LIMIT_BACKEND)
    except ImportError:
        return False  # Not a class: loading it fails loudly where it is used.
    return isinstance(backend, type) and issubclass(backend, MemoryBackend)


def _user_model(path: str) -> type[AbstractBaseUser]:
    """Import ``AUTH.USER_MODEL`` and check it is a concrete user class."""
    try:
        model = import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(f"AUTH.USER_MODEL: {exc}") from exc
    if (
        not isinstance(model, type)
        or not issubclass(model, AbstractBaseUser)
        or model in {AbstractBaseUser, AbstractUser}
    ):
        raise ImproperlyConfigured(
            f"AUTH.USER_MODEL: {path!r} must be a subclass of AbstractUser or "
            "AbstractBaseUser."
        )
    if not hasattr(model, "USERNAME_FIELD"):
        raise ImproperlyConfigured(f"AUTH.USER_MODEL: {path!r} sets no USERNAME_FIELD.")
    fields = {field.name for field in dataclasses.fields(model)}
    for listed in ("EDITABLE_FIELDS", "READ_ONLY_FIELDS"):
        if unknown := sorted(getattr(model, listed) - fields):
            raise ImproperlyConfigured(
                f"AUTH.USER_MODEL: {listed} names fields {path!r} lacks: "
                f"{', '.join(unknown)}."
            )
    if fixed := sorted(model.EDITABLE_FIELDS - model.editable_fields()):
        raise ImproperlyConfigured(
            f"AUTH.USER_MODEL: EDITABLE_FIELDS of {path!r} names fields that change "
            f"only through their own methods: {', '.join(fixed)}."
        )
    return model


def _user_table(config: AuthSettings, model: type[AbstractBaseUser]) -> Table:
    """The user model's table, with a column for every field the model stores."""
    if config.USER_TABLE is None:
        if model is not User:
            raise ImproperlyConfigured(
                "AUTH.USER_TABLE must name the table of a custom AUTH.USER_MODEL."
            )
        return users
    try:
        table = import_string(config.USER_TABLE)
    except ImportError as exc:
        raise ImproperlyConfigured(f"AUTH.USER_TABLE: {exc}") from exc
    if not isinstance(table, Table) or metadata.tables.get(table.key) is not table:
        raise ImproperlyConfigured(
            f"AUTH.USER_TABLE: {config.USER_TABLE!r} must be a Table on "
            "domainkit.db.metadata."
        )
    fields = {field.name for field in dataclasses.fields(model)} - RELATIONSHIPS
    if missing := sorted(fields - set(table.c.keys())):
        raise ImproperlyConfigured(
            f"AUTH.USER_TABLE {table.name!r} has no column for {missing}."
        )
    # Migrations and routing follow the domain, and so do the link tables.
    if table.info.get("domain") not in {config.label for config in registry}:
        raise ImproperlyConfigured(
            f"AUTH.USER_TABLE {table.name!r} needs info={{'domain': ...}} naming "
            "the installed domain that defines it."
        )
    return table
