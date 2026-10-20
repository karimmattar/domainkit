"""Install ``domainkit.contrib.auth`` (after ``domainkit.contrib.contenttypes``)."""

import dataclasses
from importlib import import_module

from fastapi import FastAPI
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.apps import DomainConfig, registry
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.contrib.contenttypes.sync import has_tables
from domainkit.db import metadata
from domainkit.db.routing import router
from domainkit.ddd import event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.module_loading import import_string

from .access import _routes, routes_need_users, unguarded_routes
from .authentication import get_authenticators
from .backends import get_backends
from .hashers import insecure_hashers
from .models import AbstractBaseUser, AbstractUser, User
from .permissions import declared_permissions, sync_permissions
from .settings import AuthSettings, auth_settings
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
)
"""Middleware that must run after it, to count users rather than addresses."""
RELATIONSHIPS = frozenset({"groups", "user_permissions"})
"""User fields stored in link tables, not in the user's own table."""


class AuthConfig(DomainConfig):
    """Users, groups, and permissions; the user model may be swapped."""

    label = "auth"
    verbose_name = "Authentication and authorization"
    settings_class = AuthSettings

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
        if not registry.is_installed(CONTENTTYPES):
            raise ImproperlyConfigured(
                f"domainkit.contrib.auth needs {CONTENTTYPES!r} in INSTALLED_DOMAINS."
            )
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
        declared_permissions()
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")
            for module in MAILING_ROUTERS:
                import_module(f"{self.name}.{module}").register_tasks()
        if registry.is_installed(PRIVACY):
            from domainkit.contrib.privacy import PersonalDataErased

            from .erasure import forget_credentials

            event_bus.subscribe(PersonalDataErased, forget_credentials)

    def check_application(self, app: FastAPI) -> list[str]:
        """Routes that declare no rule, and rules no middleware can answer."""
        problems = []
        if AUTHENTICATION_MIDDLEWARE not in settings.MIDDLEWARE and routes_need_users(
            app
        ):
            problems.append(
                f"Routes check users, but MIDDLEWARE lacks {AUTHENTICATION_MIDDLEWARE}."
            )
        if auth_settings().REQUIRE_ROUTE_RULES:
            problems.extend(
                f"{route} declares no access rule: add Depends(Require(...)), or "
                "dependencies=[Public] if anyone may call it."
                if takes_rules
                else f"{route} takes no access rule: serve it from a FastAPI route "
                "with Require(...), or wrap it in mark_public(...) if anyone may "
                "call it."
                for route, takes_rules in unguarded_routes(app)
            )
        problems.extend(self._mailing_problems(app))
        if (
            self._mounted(app, "cookies")
            and COOKIE_AUTHENTICATION not in auth_settings().AUTHENTICATION_CLASSES
        ):
            problems.append(
                f"Routes of {self.name}.cookies start browser sessions, but "
                f"AUTH.AUTHENTICATION_CLASSES lacks {COOKIE_AUTHENTICATION}."
            )
        return problems

    def _mounted(self, app: FastAPI, name: str) -> bool:
        """Whether a route of the router in module ``name`` is served by ``app``."""
        called = {dependant.call for _, dependant in _routes(app.routes) if dependant}
        module = import_module(f"{self.name}.{name}")
        return bool(called & {route.endpoint for route in module.router.routes})

    def _mailing_problems(self, app: FastAPI) -> list[str]:
        """What routes that send mail lack: the apps, or an address to send to."""
        problems = []
        for name in MAILING_ROUTERS:
            if not self._mounted(app, name):
                continue
            module = f"{self.name}.{name}"
            installed = registry.is_installed
            if missing := [name for name in (MESSAGING, MAIL) if not installed(name)]:
                problems.append(
                    f"Routes of {module} send mail: add {', '.join(missing)} "
                    "to INSTALLED_DOMAINS."
                )
            if self.user_model.EMAIL_FIELD is None or not hasattr(
                self.user_model, "verify_email"
            ):
                problems.append(
                    f"Routes of {module} need a user model with an "
                    "EMAIL_FIELD and email_verified, as AbstractUser has."
                )
        return problems

    def holds_table(self, table: Table) -> bool:
        """The default user's tables, unless a custom user replaces them."""
        return not (self.swapped and table in DEFAULT_USER_TABLES)

    async def migrated(self, connection: AsyncConnection, database: str) -> None:
        """Create the content types and permissions models declare."""
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
        cookies, and certificate proxies given as networks.
        """
        if config.CLIENT_CERT.HEADER is not None and not config.CLIENT_CERT.PROXIES:
            raise ImproperlyConfigured(
                "AUTH.CLIENT_CERT.HEADER is believed only from "
                "AUTH.CLIENT_CERT.PROXIES; list the address of the proxy that "
                "sets it."
            )
        if (
            config.CLIENT_CERT.HEADER is not None
            and config.CLIENT_CERT.MATCH == "subject"
            and config.CLIENT_CERT.VERIFY_HEADER is None
        ):
            raise ImproperlyConfigured(
                'AUTH.CLIENT_CERT: MATCH = "subject" through HEADER needs '
                "VERIFY_HEADER, or anyone could sign a certificate naming any user."
            )
        insecure = insecure_hashers(config.PASSWORD_HASHERS)
        if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
            return
        if insecure:
            raise ImproperlyConfigured(
                f"AUTH.PASSWORD_HASHERS {insecure} are for tests; remove them in "
                f"{settings.ENVIRONMENT}."
            )
        if config.COOKIE.SECURE is False:
            raise ImproperlyConfigured(
                f"AUTH.COOKIE.SECURE = False sends session cookies over plain HTTP; "
                f"remove it in {settings.ENVIRONMENT}."
            )
        if networks := [
            str(network)
            for network in config.CLIENT_CERT.PROXIES
            if network.num_addresses > 1
        ]:
            raise ImproperlyConfigured(
                f"AUTH.CLIENT_CERT.PROXIES must list single addresses (/32 or "
                f"/128) in {settings.ENVIRONMENT}, not networks "
                f"({', '.join(networks)}): any host there could name any user."
            )


def _check_middleware() -> None:
    """``AuthenticationMiddleware`` sets the actor others read: order matters."""
    order = list(settings.MIDDLEWARE)
    if AUTHENTICATION_MIDDLEWARE not in order:
        return
    position = order.index(AUTHENTICATION_MIDDLEWARE)
    for path in OUTSIDE_AUTHENTICATION:
        if path in order and order.index(path) > position:
            raise ImproperlyConfigured(
                f"MIDDLEWARE: list {path} before {AUTHENTICATION_MIDDLEWARE}."
            )
    for path in INSIDE_AUTHENTICATION:
        if path in order and order.index(path) < position:
            raise ImproperlyConfigured(
                f"MIDDLEWARE: list {AUTHENTICATION_MIDDLEWARE} before {path}, so it "
                "counts users rather than client addresses."
            )


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
