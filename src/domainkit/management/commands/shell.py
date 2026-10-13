"""``shell``: an interactive console with the project loaded."""

import code

from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.db.connections import connections
from domainkit.management.base import BaseCommand


class Command(BaseCommand):
    """Open a Python console with settings, the registry, and connections."""

    help = "Open an interactive Python console with the project set up."

    def handle(self, **options: object) -> None:
        """Start the console; use ``asyncio.run(...)`` for coroutines."""
        namespace = {
            "settings": settings,
            "registry": registry,
            "connections": connections,
        }
        banner = (
            f"{settings.PROJECT_NAME} shell ({settings.SETTINGS_MODULE}). "
            f"Available: {', '.join(namespace)}."
        )
        code.interact(banner=banner, local=namespace)
