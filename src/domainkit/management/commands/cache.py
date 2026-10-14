"""``cache``: check that caches answer, or clear them."""

from argparse import ArgumentParser

from domainkit.cache import CacheError, caches
from domainkit.conf import settings
from domainkit.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Ping caches, or delete every key of the named ones (their namespace only)."""

    help = "Check that caches answer, or clear them (--all for every cache)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the action, the aliases, and --all."""
        parser.add_argument("action", choices=("check", "clear"))
        parser.add_argument(
            "aliases",
            nargs="*",
            metavar="ALIAS",
            help="caches to act on; check defaults to all of them",
        )
        parser.add_argument(
            "--all", dest="every", action="store_true", help="clear every cache"
        )

    async def handle(
        self, *, action: str, aliases: list[str], every: bool, **options: object
    ) -> None:
        """Report each cache, and fail if any could not be reached."""
        if unknown := [alias for alias in aliases if alias not in settings.CACHES]:
            raise CommandError(f"CACHES has no {', '.join(map(repr, unknown))}.")
        if action == "clear" and not aliases and not every:
            raise CommandError("Name the caches to clear, or pass --all.")
        failed = []
        for alias in aliases or list(settings.CACHES):
            try:
                if action == "check":
                    await caches[alias].check()
                    self.write(f"{alias}: ok")
                else:
                    self.write(f"{alias}: cleared {await caches[alias].clear()} key(s)")
            except CacheError as exc:
                failed.append(alias)
                self.write(f"{alias}: unavailable ({exc})")
        if failed:
            raise CommandError(f"Unavailable: {', '.join(failed)}.")
