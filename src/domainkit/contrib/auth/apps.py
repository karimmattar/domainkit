"""Install ``domainkit.contrib.auth`` (after ``domainkit.contrib.contenttypes``)."""

import dataclasses
from collections.abc import Iterator, Mapping, Sequence
from http.cookies import SimpleCookie
from importlib import import_module
from typing import Any, Final
from uuid import UUID, uuid4

from fastapi import FastAPI, Response
from fastapi.dependencies.models import Dependant
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.apps import DomainConfig, registry
from domainkit.apps.hooks import (
    on_check_application,
    on_deployment_warnings,
    on_export_subject,
)
from domainkit.checks import CheckFailed, CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS, REDIS_SCHEMES
from domainkit.contrib.contenttypes.sync import has_tables
from domainkit.db import SqlAlchemyUnitOfWork, metadata
from domainkit.db.hooks import on_committed, on_migrated, on_stage
from domainkit.db.routing import router
from domainkit.ddd import DomainEvent, event_bus
from domainkit.ddd.fields import fields_of
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import (
    AllOf,
    AnyOf,
    BasePermission,
    Not,
    Require,
    routes_need_users,
    served_routes,
)
from domainkit.http.live import streams
from domainkit.http.ratelimit import MemoryBackend
from domainkit.utils.module_loading import import_string
from domainkit.utils.rates import PERIODS, parse_rate

from .access import StepUp
from .authentication import get_authenticators
from .backends import (
    backends_without_object_grants,
    forget_permissions,
    get_backends,
)
from .hashers import insecure_hashers
from .models import HIDDEN_FIELDS, PROTECTED_FLOOR, AbstractBaseUser, AbstractUser, User
from .permissions import declared_permissions, sync_permissions
from .principals import invalidate_all, stamp_committed, stamp_pending, watch
from .routing import AccountRoutes, on_account_routes
from .settings import PRINCIPAL_TTL, AuthSettings, auth_settings
from .tables import (
    DEFAULT_USER_TABLES,
    lookup_indexes,
    permissions,
    start_mappers,
    users,
)
from .validators import get_password_validators, preset_contradictions

CONTENTTYPES = "domainkit.contrib.contenttypes"
NOTIFICATIONS = "domainkit.contrib.notifications"
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
        """Load the user model, check the configuration, and map the tables.

        With ``ADMIN_ACTION`` on, the action ``auth.admin_change`` is
        registered: each change of the admin API asks for an authorization.
        """
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
        on_committed(self.after_commit)
        on_migrated(self.create_permissions)
        self._leave_lookup_indexes()
        on_migrated(self.index_lookups)
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
        on_export_subject(_export_logins, domain=self.label)
        from .actions import CODE_CONFIRMATION, on_action_confirmation, register_action

        on_action_confirmation(CODE_CONFIRMATION, domain=self.label)
        if config.ADMIN_ACTION:  # Each change of the admin API is an act (ADR 0163).
            from .admin_actions import ADMIN_CHANGE

            register_action(ADMIN_CHANGE, domain=self.label)
        on_check_application(self.application_problems)
        on_deployment_warnings(self.weak_settings)
        on_account_routes(self.account_routes, domain=self.label)

    def account_routes(self) -> list[AccountRoutes]:
        """Auth's routes for ``account_router`` (``routing``).

        Sessions are on with ``CookieAuthentication``, recovery with the
        apps that send its mail, and signup only when a project includes it.
        """
        cookies = COOKIE_AUTHENTICATION in auth_settings().AUTHENTICATION_CLASSES
        mails = registry.is_installed(MESSAGING) and registry.is_installed(MAIL)
        return [
            AccountRoutes("login", f"{self.name}.api.router"),
            AccountRoutes("sessions", f"{self.name}.cookies.router", default=cookies),
            AccountRoutes("mfa", f"{self.name}.mfa.router"),
            AccountRoutes("tokens", f"{self.name}.personal_tokens.router"),
            AccountRoutes("step-up", f"{self.name}.stepup.router"),
            AccountRoutes("sca", f"{self.name}.sca.router"),
            AccountRoutes("authorizations", f"{self.name}.actions.router"),
            AccountRoutes("recovery", f"{self.name}.recovery.router", default=mails),
            AccountRoutes("signup", f"{self.name}.signup.router", default=False),
        ]

    async def stamp_changes(
        self,
        uow: SqlAlchemyUnitOfWork,
        events: Sequence[DomainEvent],
        tasks: Sequence[Any],
    ) -> None:
        """Stamp what the commit will change in principals, before it too."""
        await stamp_pending(uow.session, auth_settings().PRINCIPAL_CACHE)

    async def after_commit(self, uow: SqlAlchemyUnitOfWork) -> None:
        """Forget memoised permissions; wait for the stamps of what changed.

        What backends answered within the request may no longer hold: the
        commit may have granted, revoked, or deleted what they read
        (``backends.memoised``). The stamps tell other processes.
        """
        forget_permissions()
        await stamp_committed(uow.session)

    async def issue_test_token(
        self, permissions: Sequence[str], fields: dict[str, Any]
    ) -> str:
        """Store a new user granted ``permissions``; its access token (``testing``).

        ``fields`` set the user's fields; the username is unique unless
        given. The token comes from the first ``AUTH.AUTHENTICATION_CLASSES``
        class, as a login's would.

        Raises:
            ImproperlyConfigured: ``ENVIRONMENT`` is not ``"test"``.
        """
        from .testing import fill_identifier
        from .uow import AuthUnitOfWork

        _only_in_tests("issue_test_token")
        username = fields.pop(self.user_model.USERNAME_FIELD, f"user-{uuid4().hex}")
        fill_identifier(self.user_model, username, fields)
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
            ImproperlyConfigured: ``ENVIRONMENT`` is not ``"test"``.
            NotFound: No user has the id.
        """
        from .uow import AuthUnitOfWork

        _only_in_tests("issue_test_token_for")
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(user_id)
            issued = await get_authenticators()[0].issue(uow, user)
            await uow.commit()
        return issued.access_token

    async def issue_test_session(self, user_id: Any) -> dict[str, str]:  # noqa: ANN401 - any user model's id.
        """Headers of a browser signed in as the stored user ``user_id`` (``testing``).

        The session starts as ``POST /auth/session`` starts one, without the
        password: its cookie and the CSRF cookie go in ``Cookie``, and the
        CSRF token in the header unsafe methods need, as a page's script
        sends it. There is no route to it.

        Raises:
            ImproperlyConfigured: ``ENVIRONMENT`` is not ``"test"``, or
                ``AUTH.AUTHENTICATION_CLASSES`` lacks ``CookieAuthentication``.
            NotFound: No user has the id.
        """
        from .cookies import cookie_names, start_session
        from .gates import gates_passed
        from .uow import AuthUnitOfWork

        _only_in_tests("issue_test_session")
        config = auth_settings()
        if COOKIE_AUTHENTICATION not in config.AUTHENTICATION_CLASSES:
            raise ImproperlyConfigured(
                f"A browser session needs {COOKIE_AUTHENTICATION} in "
                f"AUTH.AUTHENTICATION_CLASSES."
            )
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(user_id)
        response = Response()
        with gates_passed(user.id):  # A test's session: no request to judge.
            started = await start_session(user, response)
        cookies: SimpleCookie = SimpleCookie()
        for header in response.headers.getlist("set-cookie"):
            cookies.load(header)
        sent = "; ".join(f"{name}={cookies[name].value}" for name in cookie_names())
        return {"Cookie": sent, config.COOKIE.CSRF_HEADER: started.csrf_token}

    async def confirm_test_authorization(
        self,
        authorization_id: Any,  # noqa: ANN401 - a UUID, or its text.
        methods: Sequence[str],
    ) -> str:
        """Confirm an action authorization as proved by ``methods`` (``testing``).

        What the user's phone, a passkey, or a code would prove, without
        them: ``methods`` are what the act asks (its twin's ``methods``).
        Returns ``<id>.<code>``, for the act's ``Action-Authorization``.

        Raises:
            ImproperlyConfigured: ``ENVIRONMENT`` is not ``"test"``.
            AuthorizationNotFound: No authorization has the id.
            AuthorizationNeedsOtherFactors: ``methods`` do not prove what
                the act asks.
            APIError: 409 ``authorization_confirmed`` or
                ``authorization_expired``.
        """
        from .actions import AuthorizationNotFound, confirm_action_with
        from .uow import AuthUnitOfWork

        _only_in_tests(
            "confirm_test_authorization", "confirms acts without a second factor"
        )
        async with AuthUnitOfWork() as uow:
            found = await uow.authorizations.get(UUID(str(authorization_id)))
        if found is None:
            raise AuthorizationNotFound("No such authorization.")
        return await confirm_action_with(found.id, methods, user_id=found.user_id)

    def application_problems(self, app: FastAPI) -> list[str]:
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
        problems.extend(
            CheckMessage(
                "auth.E106",
                f"{route} asks StepUp for {', '.join(sorted(missing))}, which no "
                f"step-up proves (AUTH.STEP_UP.METHODS is "
                f"{list(auth_settings().STEP_UP.METHODS)}): it refuses everyone. "
                "Add the methods to AUTH.STEP_UP.METHODS, or leave methods out.",
            )
            for route, missing in unprovable_step_ups(app)
        )
        problems.extend(action_problems(app))
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

    def _texts_codes(self) -> bool:
        """Whether resets and signups send codes to numbers (ADR 0126)."""
        config = auth_settings()
        return "mobile_number" in (config.IDENTIFIERS or ())

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
            if self._texts_codes() and not installed(NOTIFICATIONS):
                problems.append(
                    CheckMessage(
                        "auth.E107",
                        f"Routes of {module} send codes to mobile numbers by text "
                        f"message: add {NOTIFICATIONS} to INSTALLED_DOMAINS.",
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

    def weak_settings(self) -> list[str]:
        """Weak choices in staging and production.

        Login and code checks without a rate, or counted in each process
        (with ``AUTH.REQUIRE_SHARED_THROTTLES = False``; refused otherwise),
        cookies without ``__Host-`` where it could hold, and principals
        kept longer than ``PRINCIPAL_TTL`` where nothing tells other
        processes of changes, a captcha without ``HOSTNAMES``
        (``auth.W008``), and a worker that may send codes once they expired
        (``auth.W009``). Anywhere, a backend that does not list its grants
        on objects (``auth.W007``), and members' keys without the list of a
        user's tenants (``auth.W010``): only superusers then change users.
        """
        unlisted: list[str] = [
            CheckMessage(
                "auth.W007",
                f"AUTH.BACKENDS {backend} does not list its grants on objects "
                "(object_grants), so only superusers change users and groups "
                "through the admin API; override object_grants, with an empty "
                "set if it grants on no object.",
            )
            for backend in backends_without_object_grants()
        ]
        tenancy = settings.TENANCY
        if (
            tenancy is not None
            and tenancy.MEMBER_PERMISSIONS is not None
            and tenancy.MEMBER_TENANTS is None
        ):
            unlisted.append(
                CheckMessage(
                    "auth.W010",
                    "TENANCY.MEMBER_PERMISSIONS is set without MEMBER_TENANTS, so "
                    "only superusers change users through the admin API: name "
                    "the function that lists the tenants a user belongs to.",
                )
            )
        if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
            return unlisted
        config = auth_settings()
        cookie = config.COOKIE
        warnings: list[str] = list(unlisted)
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
        if _over_a_day(config.ACCOUNT_LOGIN_RATE):
            warnings.append(
                CheckMessage(
                    "auth.W006",
                    f"AUTH.ACCOUNT_LOGIN_RATE {config.ACCOUNT_LOGIN_RATE!r} lets more "
                    f"than {NIST_DAILY_GUESSES} wrong passwords a day at one account, "
                    "past NIST SP 800-63B 5.2.2; set it to "
                    f'"{NIST_DAILY_GUESSES}/day" or less.',
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
        warnings.extend(_late_codes(config.CODE_TTL.total_seconds()))
        if config.CAPTCHA is not None and not config.CAPTCHA.HOSTNAMES:
            warnings.append(
                CheckMessage(
                    "auth.W008",
                    "AUTH.CAPTCHA.HOSTNAMES is empty, so a token solved on "
                    "another site that shows your site key passes; name your "
                    "hostnames, or restrict the key's domains at the provider.",
                )
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

    async def index_lookups(self, connection: AsyncConnection, database: str) -> None:
        """Make the user lookups' indexes after ``migrate``, on PostgreSQL.

        ``lower()`` of the address and the username, and the admin's order
        (``lookup_indexes``), each where missing. Not on SQLite, which
        cannot reflect an index on ``lower()``: such an index would warn at
        every ``makemigrations`` and snapshot, so they are not declared on
        the table either, and revisions leave them alone (A28-PERF-02).
        """
        if database != self.database or connection.dialect.name != "postgresql":
            return
        if not await has_tables(connection, self.user_table):
            return
        preparer = connection.dialect.identifier_preparer
        table = preparer.format_table(self.user_table)
        wanted = lookup_indexes(self.user_model, self.user_table, preparer.quote)
        for name, indexed in wanted.items():
            await connection.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS {preparer.quote(name)} "
                f"ON {table} ({indexed})"
            )

    def _leave_lookup_indexes(self) -> None:
        """Have revisions leave the lookups' indexes alone (``unmanaged_indexes``)."""
        info = self.user_table.info
        names = lookup_indexes(self.user_model, self.user_table)
        info["unmanaged_indexes"] = (
            *dict.fromkeys((*info.get("unmanaged_indexes", ()), *names)),
        )

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
        names = {field.name for field in dataclasses.fields(self.user_model)}
        identifiers = config.IDENTIFIERS or ()
        if unknown := [name for name in identifiers if name not in names]:
            errors.append(
                CheckMessage(
                    "auth.E011",
                    f"AUTH.IDENTIFIERS names {', '.join(map(repr, unknown))}, which "
                    f"{self.user_model.__name__} has no field for; name its fields, "
                    "such as 'email' or 'mobile_number'.",
                )
            )
        if len(set(identifiers)) != len(identifiers):
            errors.append(
                CheckMessage(
                    "auth.E011",
                    "AUTH.IDENTIFIERS names a field twice: name each once.",
                )
            )
        errors.extend(self._name_field_errors(config))
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
        if contradictions := preset_contradictions(config):
            errors.append(
                CheckMessage(
                    "auth.E010",
                    f"AUTH.PRESET {config.PRESET!r} is "
                    f"contradicted: {'; '.join(contradictions)}. Leave these "
                    "settings out for the preset's, or drop the preset.",
                )
            )
        if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
            errors.extend(self._deployed_errors(config))
        return errors

    def _name_field_errors(self, config: AuthSettings) -> list[CheckMessage]:
        """What ``auth.E012`` refuses of ``AUTH.NAME_FIELDS``: one message a case.

        A name is built of text that ``GET /users`` shows: a field among a
        user's ``fields`` (the model's ``admin_fields()``, never one of
        ``HIDDEN_FIELDS``), or the username, its own key there.
        """
        if config.NAME_FIELDS is None:
            return []
        model = self.user_model
        cases: dict[str, list[str]] = {}
        for name in dict.fromkeys(
            each for entry in config.NAME_FIELDS for each in entry
        ):
            if case := _name_field_case(model, name):
                cases.setdefault(case, []).append(name)
        what = model.__name__
        advice = {
            "missing": f"which {what} has no field for: name fields of {what}.",
            "hidden": (
                "which the admin API never shows (credentials, grants, ids): "
                "name fields GET /users shows."
            ),
            "unshown": (
                f"which GET /users does not show of {what}: add them to "
                f"{what}.EDITABLE_FIELDS or READ_ONLY_FIELDS, or name fields "
                "it shows."
            ),
            "not_text": "which do not hold text: a name is built of text fields only.",
        }
        return [
            CheckMessage(
                "auth.E012",
                f"AUTH.NAME_FIELDS names {', '.join(map(repr, names))}, {text}",
            )
            for case, text in advice.items()
            if (names := cases.get(case))
        ]

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


def _name_field_case(model: type[AbstractBaseUser], name: str) -> str | None:
    """Why ``name`` cannot name ``model``'s users (``auth.E012``); None if it can."""
    fields = fields_of(model)
    if name not in fields:
        return "missing"
    if name in HIDDEN_FIELDS:
        return "hidden"
    if name != model.USERNAME_FIELD and name not in model.admin_fields():
        return "unshown"
    kind = fields[name].kind
    if not (isinstance(kind, type) and issubclass(kind, str)):
        return "not_text"
    return None


NIST_DAILY_GUESSES: Final = 100
"""Wrong passwords NIST SP 800-63B 5.2.2 lets one account take: 100 at most."""


def _late_codes(ttl: float) -> list[str]:
    """``auth.W009``: messaging may send a code after half its life is gone.

    A code whose worker died mid-send runs again after
    ``MESSAGING.VISIBILITY_TIMEOUT``, and one a broker lost is published
    again after ``REPUBLISH_AFTER``: either should leave the code most of
    ``AUTH.CODE_TTL`` to be used. Nothing without messaging installed.
    """
    if not registry.is_installed("domainkit.contrib.messaging"):
        return []
    from domainkit.contrib.messaging import MessagingSettings

    messaging = registry.settings(MessagingSettings)
    late = [("VISIBILITY_TIMEOUT", messaging.VISIBILITY_TIMEOUT)]
    if messaging.BROKER is not None:
        late.append(("REPUBLISH_AFTER", messaging.REPUBLISH_AFTER))
    return [
        CheckMessage(
            "auth.W009",
            f"MESSAGING.{name} ({seconds:g} s) is more than half of AUTH.CODE_TTL "
            f"({ttl:g} s): a code whose sending was cut short is sent again "
            f"too late to be used; set it to {ttl / 2:g} s or less.",
        )
        for name, seconds in late
        if seconds > ttl / 2
    ]


def _over_a_day(rate: str | None) -> bool:
    """Whether ``rate`` lets more than ``NIST_DAILY_GUESSES`` through in a day."""
    if rate is None:
        return True
    limit, period = parse_rate(rate)
    return limit * (PERIODS["day"] // period) > NIST_DAILY_GUESSES


def unprovable_step_ups(app: FastAPI) -> list[tuple[str, frozenset[str]]]:
    """Each route whose ``StepUp`` asks for methods no step-up proves, and them.

    ``StepUp(methods=...)`` beyond ``AUTH.STEP_UP.METHODS`` refuses everyone:
    ``POST /step-up`` proves those alone. Rules are found in ``Require`` at
    any depth of a route's dependencies, inside ``&``, ``|``, and ``~``.
    """
    found = []
    for route, dependant in served_routes(app.routes):
        missing: frozenset[str] = frozenset()
        for rule in _rules_of(dependant):
            if isinstance(rule, StepUp):
                missing |= rule.unprovable()
        if missing:
            found.append((route, missing))
    return found


def action_problems(app: FastAPI) -> list[CheckMessage]:
    """What ``check`` refuses of action authorizations (``auth.E108`` to ``E112``).

    An action no installed way to confirm proves (``E108``); a route that
    asks for a purpose no registered action has (``E109``), or whose action
    nothing spends (``E110``: no decorated service, and no code that said
    so with ``spent_in_code``); ``StepUp(for_=...)`` on a WebSocket or
    an event stream (``E111``); and ``AUTH.AUTHORIZATIONS.ACTIONS`` naming
    a purpose no registered action has (``E112``), whose settings would
    hold for nothing.
    """
    from .actions import provable, registered_actions

    actions = registered_actions()
    problems = [
        CheckMessage(
            "auth.E112",
            f"AUTH.AUTHORIZATIONS.ACTIONS names {purpose!r}, which no registered "
            "action has: its TTL and METHODS would hold for nothing. Correct the "
            "purpose, turn on the setting that registers it, or remove it.",
        )
        for purpose in sorted(auth_settings().AUTHORIZATIONS.ACTIONS)
        if purpose not in actions
    ]
    problems += [
        CheckMessage(
            "auth.E108",
            f"Action {purpose!r} asks confirming it to prove "
            f"{', '.join(sorted(action.required()))}, which no installed way to "
            "confirm proves: nobody could confirm it. Install the app that "
            "confirms it (the phone: domainkit.contrib.ciba or "
            "domainkit.contrib.devices), or change its methods.",
        )
        for purpose, action in actions.items()
        if not provable(action.required())
    ]
    for route, dependant in served_routes(app.routes):
        for rule in _rules_of(dependant):
            if isinstance(rule, StepUp) and rule.purpose is not None:
                problems.extend(_asking_problems(route, dependant, rule.purpose))
    return problems


def _asking_problems(
    route: str, dependant: Dependant | None, purpose: str
) -> list[CheckMessage]:
    """What is wrong with ``route`` asking for an authorization of ``purpose``."""
    from .actions import registered_actions, spent_by_a_service

    problems = []
    if route.startswith("WEBSOCKET ") or streams(dependant):
        problems.append(
            CheckMessage(
                "auth.E111",
                f"{route} asks StepUp(for_={purpose!r}), but it keeps a "
                "connection open, which is asked again and again and has no act "
                "to spend an authorization on. Use StepUp(max_age=...) there.",
            )
        )
    if purpose not in registered_actions():
        problems.append(
            CheckMessage(
                "auth.E109",
                f"{route} asks for an authorization of {purpose!r}, which no "
                "registered action has: register_action(...) it in ready().",
            )
        )
    elif not spent_by_a_service(purpose):
        problems.append(
            CheckMessage(
                "auth.E110",
                f"{route} asks for an authorization of {purpose!r}, which "
                "nothing spends: decorate the act's service with "
                "@requires_authorization(...), or, where code of yours calls "
                "spend_authorization, say so with spent_in_code(...).",
            )
        )
    return problems


def _rules_of(dependant: Dependant | None) -> Iterator[BasePermission]:
    """Every rule the ``Require`` dependencies of ``dependant`` check, nested too."""
    if dependant is None:
        return
    if isinstance(dependant.call, Require):
        for rule in dependant.call.rules:
            yield from _nested(rule)
    for each in dependant.dependencies:
        yield from _rules_of(each)


def _nested(rule: BasePermission) -> Iterator[BasePermission]:
    """``rule``, and the rules it combines (``&``, ``|``, ``~``)."""
    yield rule
    inner = (
        rule.rules
        if isinstance(rule, AllOf | AnyOf)
        else (rule.rule,)
        if isinstance(rule, Not)
        else ()
    )
    for each in inner:
        yield from _nested(each)


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
    if unknown := sorted(set(model.REQUIRED_FIELDS) - fields):
        raise ImproperlyConfigured(
            f"AUTH.USER_MODEL: REQUIRED_FIELDS names fields {path!r} lacks: "
            f"{', '.join(unknown)}."
        )
    if model.LANGUAGE_FIELD is not None and model.LANGUAGE_FIELD not in fields:
        raise ImproperlyConfigured(
            f"AUTH.USER_MODEL: LANGUAGE_FIELD names a field {path!r} lacks: "
            f"{model.LANGUAGE_FIELD}."
        )
    protected = model.PROTECTED_FIELDS | PROTECTED_FLOOR
    if fixed := sorted(set(model.signup_fields()) & protected):
        raise ImproperlyConfigured(
            f"AUTH.USER_MODEL: REQUIRED_FIELDS of {path!r} names fields that change "
            f"only through their own methods: {', '.join(fixed)}."
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


def _only_in_tests(what: str, does: str = "signs users in without credentials") -> None:
    """Refuse to sign anyone in without credentials outside the test settings.

    The test tokens skip the password, the second factor, and the login's
    audit record: in any other process they would be a bearer token for
    anyone, a superuser included (SEC-12a-2). So would an act confirmed
    without its second factor. The database fixtures refuse such settings
    for the same reason.

    Raises:
        ImproperlyConfigured: ``ENVIRONMENT`` is not ``"test"``.
    """
    if settings.ENVIRONMENT != "test":
        raise ImproperlyConfigured(
            f"{what} {does}, so it needs "
            f'ENVIRONMENT = "test", not {settings.ENVIRONMENT!r}.'
        )


async def _export_logins(subject: str) -> Mapping[str, object]:
    """What auth keeps of ``subject``'s logins (``privacy export``)."""
    from .erasure import export_logins

    return await export_logins(subject)
