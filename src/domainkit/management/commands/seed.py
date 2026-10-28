"""``seed``: run installed domains' ``on_seed`` hooks (sample data, defaults)."""

from argparse import ArgumentParser

from domainkit.apps import registry
from domainkit.apps.hooks import on_seed
from domainkit.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Run the ``on_seed`` hooks of every installed domain, or those named.

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
        seeded: list[str] = []
        for hook in on_seed:
            if labels and hook.domain not in labels:
                continue
            await hook.call()
            if hook.domain not in seeded:
                seeded.append(hook.domain)
        self.write(
            f"Seeded {', '.join(seeded)}." if seeded else "No domain has seed data."
        )
