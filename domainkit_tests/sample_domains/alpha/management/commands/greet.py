"""``greet``: an async domain command that reads domain settings."""

from argparse import ArgumentParser

from domainkit.apps import registry
from domainkit.management import BaseCommand
from domainkit_tests.sample_domains.alpha.apps import AlphaSettings


class Command(BaseCommand):
    """Greet someone with the configured greeting."""

    help = "Print the alpha greeting."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept a name and a switch."""
        parser.add_argument("name")
        parser.add_argument("--shout", action="store_true")

    async def handle(self, *, name: str, shout: bool, **options: object) -> None:
        """Write the greeting."""
        text = f"{registry.settings(AlphaSettings).GREETING}, {name}"
        self.write(text.upper() if shout else text)
