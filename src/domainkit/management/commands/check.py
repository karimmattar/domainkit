"""``check``: validate the project without starting a server.

Every finding is a ``domainkit.checks.CheckMessage`` with an id. Errors
fail the command, all of them listed at once; warnings are listed, except
those ``SILENCED_CHECKS`` names. ``check --deploy`` checks a staging or
production configuration for a deployment pipeline: there, warnings fail
the command too.
"""

from argparse import ArgumentParser
from collections.abc import Iterable

from fastapi import FastAPI

from domainkit.apps import registry
from domainkit.architecture import check_domains
from domainkit.cache.handler import deployment_warnings as cache_deployment_warnings
from domainkit.checks import CheckMessage, describe, silence
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db import migrations
from domainkit.db.connections import pool_sizing_warnings
from domainkit.http import websockets
from domainkit.http.application import application_problems, build_application
from domainkit.http.blocking import sync_calls
from domainkit.http.servers import unix_socket_warnings
from domainkit.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Load settings, domains, routes, and middleware; enforce layer rules."""

    help = (
        "Validate settings, installed domains, routes and their access rules, "
        "middleware, the dependency rules between domain layers, and the "
        "migration history; warn about route handlers and dependencies that "
        "run on the thread pool, and about weak choices in staging and "
        "production. With --deploy, warnings fail too."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """``--deploy``: fail on warnings, for a deployment pipeline."""
        parser.add_argument(
            "--deploy",
            action="store_true",
            help=(
                "check a staging or production configuration: warnings not "
                "in SILENCED_CHECKS fail the command too"
            ),
        )

    def handle(self, *, deploy: bool = False, **options: object) -> None:
        """Fail with every problem found, or report success."""
        if deploy and settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
            raise CommandError(
                "check --deploy checks a deployed configuration; run it with "
                f"ENVIRONMENT set to staging or production, not "
                f"{settings.ENVIRONMENT!r}."
            )
        app = build_application()
        problems = application_problems(app)
        problems.extend(
            CheckMessage("architecture.E001", str(violation))
            for violation in check_domains(settings.INSTALLED_DOMAINS)
        )
        problems.extend(
            CheckMessage(
                "migrations.E001",
                f"Migrations of {target!r} have {len(heads)} heads "
                f"({', '.join(sorted(heads))}); run 'manage.py makemigrations "
                "--merge'.",
            )
            for target in migrations.migration_targets()
            if len(heads := migrations.heads(target)) > 1
        )
        if problems:
            raise CommandError(_listed("System check found problems", problems))
        found = _warnings(app)
        warnings = silence(found, settings.SILENCED_CHECKS)
        if deploy and warnings:
            raise CommandError(
                _listed("System check --deploy refuses these warnings", warnings)
            )
        labels = ", ".join(config.label for config in registry) or "none"
        plural = "s" if len(warnings) > 1 else ""
        counted = f" and {len(warnings)} warning{plural}" if warnings else ""
        if silenced := len(found) - len(warnings):
            counted += f" ({silenced} silenced)"
        self.write(f"System check identified no issues{counted}. Domains: {labels}.")
        for warning in warnings:
            self.write(describe(warning, label="Warning: "))


def _warnings(app: FastAPI) -> list[str]:
    """Every warning, the framework's first, then the domains', each with an id."""
    deployed = settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS
    warnings: list[str] = list(settings.deployment_warnings())
    warnings += _tagged("caches.W001", cache_deployment_warnings())
    warnings += _tagged("deploy.W006", pool_sizing_warnings())
    warnings += _tagged("live.W001", websockets.deployment_warnings(app))
    warnings += [found for config in registry for found in config.deployment_warnings()]
    if deployed:
        warnings += _tagged("servers.W001", websockets.server_warnings(app))
        warnings += _tagged(
            "servers.W002", unix_socket_warnings(settings.TRUSTED_PROXIES)
        )
    warnings += _tagged("blocking.W001", sync_calls(app, _project_domains()))
    return warnings


def _tagged(check_id: str, messages: Iterable[str]) -> list[str]:
    """``messages`` of one check, given its id."""
    return [CheckMessage(check_id, message) for message in messages]


def _listed(heading: str, messages: list[str]) -> str:
    """``heading``, then each message with its id and hint, one per line."""
    return f"{heading}:\n" + "\n".join(describe(message) for message in messages)


def _project_domains() -> list[str]:
    """The installed domains that are the project's, not domainkit's own."""
    return [
        name for name in settings.INSTALLED_DOMAINS if not name.startswith("domainkit.")
    ]
