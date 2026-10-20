"""``seed``: run installed domains' ``seed`` hooks (sample data, defaults)."""

from argparse import ArgumentParser

from domainkit.apps import DomainConfig, registry
from domainkit.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Call ``DomainConfig.seed`` of every installed domain, or those named.

    Domains run in ``INSTALLED_DOMAINS`` order. A seed must be safe to run
    again: rows that exist stay as they are.
    """

    help = "Run the seed hooks of installed domains, in INSTALLED_DOMAINS order."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept domain labels."""
        parser.add_argument(
            "labels", nargs="*", metavar="LABEL", help="domains to seed (default: all)"
        )

    async def handle(self, *, labels: list[str], **options: object) -> None:
        """Seed each chosen domain that has a seed hook."""
        installed = {config.label for config in registry}
        if unknown := [label for label in labels if label not in installed]:
            raise CommandError(f"No installed domain {', '.join(map(repr, unknown))}.")
        seeded = []
        for config in registry:
            if labels and config.label not in labels:
                continue
            if type(config).seed is DomainConfig.seed:
                continue
            await config.seed()
            seeded.append(config.label)
        self.write(
            f"Seeded {', '.join(seeded)}." if seeded else "No domain has seed data."
        )
