"""``cache``: check that caches answer, or clear them."""

from domainkit.cache import CacheError, caches
from domainkit.conf import settings
from domainkit.management.actions import ActionCommand, action, option
from domainkit.management.base import CommandError


class Command(ActionCommand):
    """Ping caches, or delete every key of the named ones (their namespace only)."""

    help = "Check that caches answer, or clear them (--all for every cache)."

    @action(
        help="ping caches (default: all of them)",
        options=[option("aliases", nargs="*", metavar="ALIAS")],
    )
    async def check(self, *, aliases: list[str]) -> None:
        """Report each cache, and fail if any could not be reached."""
        await self._each(_known(aliases), clear=False)

    @action(
        help="delete every key of the named caches, all versions, in their "
        "namespace only",
        options=[
            option("aliases", nargs="*", metavar="ALIAS"),
            option("--all", dest="every", action="store_true", help="every cache"),
        ],
    )
    async def clear(self, *, aliases: list[str], every: bool) -> None:
        """Clear the caches named, or every one with ``--all``.

        Raises:
            CommandError: Neither aliases nor ``--all``.
        """
        if not _known(aliases) and not every:
            raise CommandError("Name the caches to clear, or pass --all.")
        await self._each(aliases, clear=True)

    async def _each(self, aliases: list[str], *, clear: bool) -> None:
        """Check or clear each cache named (default: every one).

        Raises:
            CommandError: A cache could not be reached.
        """
        failed = []
        for alias in aliases or list(settings.CACHES):
            try:
                if clear:
                    self.write(f"{alias}: cleared {await caches[alias].clear()} key(s)")
                else:
                    await caches[alias].check()
                    self.write(f"{alias}: ok")
            except CacheError as exc:
                failed.append(alias)
                self.write(f"{alias}: unavailable ({exc})")
        if failed:
            raise CommandError(f"Unavailable: {', '.join(failed)}.")


def _known(aliases: list[str]) -> list[str]:
    """``aliases``, if ``CACHES`` has each.

    Raises:
        CommandError: One it has not.
    """
    if unknown := [alias for alias in aliases if alias not in settings.CACHES]:
        raise CommandError(f"CACHES has no {', '.join(map(repr, unknown))}.")
    return aliases
