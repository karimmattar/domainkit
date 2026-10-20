"""``check``: validate the project without starting a server."""

from domainkit.apps import registry
from domainkit.architecture import check_domains
from domainkit.conf import settings
from domainkit.db import migrations
from domainkit.http import get_asgi_application
from domainkit.http.application import application_problems
from domainkit.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Load settings, domains, routes, and middleware; enforce layer rules."""

    help = (
        "Validate settings, installed domains, routes and their access rules, "
        "middleware, the dependency rules between domain layers, and the "
        "migration history; warn about weak choices in staging and production."
    )

    def handle(self, **options: object) -> None:
        """Fail with every problem found, or report success."""
        app = get_asgi_application()
        problems = [f"Application: {problem}" for problem in application_problems(app)]
        problems.extend(
            f"Architecture: {violation}"
            for violation in check_domains(settings.INSTALLED_DOMAINS)
        )
        problems.extend(
            f"Migrations of {target!r} have {len(heads)} heads "
            f"({', '.join(sorted(heads))}); run 'manage.py makemigrations --merge'."
            for target in migrations.migration_targets()
            if len(heads := migrations.heads(target)) > 1
        )
        if problems:
            raise CommandError(
                "System check found problems:\n"
                + "\n".join(f"  {problem}" for problem in problems)
            )
        labels = ", ".join(config.label for config in registry) or "none"
        warnings = settings.deployment_warnings()
        warnings += [
            found for config in registry for found in config.deployment_warnings()
        ]
        plural = "s" if len(warnings) > 1 else ""
        found = f" and {len(warnings)} warning{plural}" if warnings else ""
        self.write(f"System check identified no issues{found}. Domains: {labels}.")
        for warning in warnings:
            self.write(f"  Warning: {warning}")
