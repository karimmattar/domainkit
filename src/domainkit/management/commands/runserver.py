"""``runserver``: a development server for ``ASGI_APPLICATION``: uvicorn or Granian.

Each is imported only when it serves: Granian serves without uvicorn,
which becomes an extra in 3.0.
"""

import copy
from argparse import ArgumentParser
from typing import Final

from domainkit.checks import CheckMessage, describe, silence
from domainkit.conf import settings
from domainkit.management.base import BaseCommand, CommandError
from domainkit.utils.loops import loop_factory

SERVERS: Final = ("uvicorn", "granian")

DEFAULT_ADDRESS = "127.0.0.1"
DEFAULT_PORT = 8000


def parse_address(value: str, host: str = DEFAULT_ADDRESS) -> tuple[str, int]:
    """Parse ``port`` (on ``host``), ``host:port``, or ``[ipv6]:port``."""
    port = value
    if not value.isdigit():
        host, _, port = value.rpartition(":")
        host = host.removeprefix("[").removesuffix("]")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise CommandError(f"{value!r} is not a valid port or address:port.")
    return host, int(port)


class Command(BaseCommand):
    """Run uvicorn, or Granian, with auto-reload; production runs ``serve``."""

    help = "Start a development server for ASGI_APPLICATION."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept an optional address and a reload switch."""
        parser.add_argument(
            "address",
            nargs="?",
            default=f"{DEFAULT_ADDRESS}:{DEFAULT_PORT}",
            help="port or address:port (default: %(default)s)",
        )
        parser.add_argument(
            "--noreload",
            action="store_false",
            dest="reload",
            help="do not restart when code changes",
        )
        parser.add_argument(
            "--server",
            choices=SERVERS,
            help="the ASGI server (default: RUNSERVER_SERVER, uvicorn unless set; "
            "granian needs its extra)",
        )

    def handle(
        self,
        *,
        address: str,
        reload: bool,
        server: str | None = None,
        **options: object,
    ) -> None:
        """Start the server; framework logging replaces the server's own.

        It first says which databases lack migrations (``migrations.W001``),
        as a request that uses their tables would fail. The server runs the
        application's lifespan, and stops if it fails: a domain that cannot
        start, or a middleware that refuses the configuration.
        """
        if not settings.ASGI_APPLICATION:
            raise CommandError("Set ASGI_APPLICATION, e.g. 'config.asgi:application'.")
        host, port = parse_address(address)
        loop_factory(settings.EVENT_LOOP)  # Refuse a loop that is not installed.
        from domainkit.db import migrations  # Alembic, for this check alone.

        found = [
            CheckMessage("migrations.W001", warning)
            for warning in migrations.unapplied_warnings()
        ]
        for warning in silence(found, settings.SILENCED_CHECKS):
            self.note(describe(warning, indent="", label="Warning: "))
        if (server or settings.RUNSERVER_SERVER) == "granian":
            serve_with_granian(settings.ASGI_APPLICATION, host, port, reload=reload)
            return
        import uvicorn

        uvicorn.run(
            settings.ASGI_APPLICATION,
            host=host,
            port=port,
            reload=reload,
            loop=settings.EVENT_LOOP,
            # A lifespan that raises stops the server; "auto" logged it at
            # INFO as unsupported and served every request a 500.
            lifespan="on",
            log_config=None,
            access_log=False,
            # ProxyHeadersMiddleware and TRUSTED_PROXIES handle forwarding.
            proxy_headers=False,
            server_header=False,
        )


def serve_with_granian(target: str, host: str, port: int, *, reload: bool) -> None:
    """Serve ``target`` (``module:attribute``) with Granian's ASGI interface.

    Granian's own lines go through ``LOGGING``, as the application's do. It
    gets a copy, as it sets its logger's level in the mapping it is given.
    Its access log stays off, since ``domainkit.access`` writes one. Its
    loop is ``EVENT_LOOP``'s, resolved here: Granian's own ``auto`` would
    take rloop before uvloop.

    Raises:
        CommandError: Granian is not installed.
    """
    try:
        from granian import Granian
        from granian.constants import Interfaces, Loops
    except ImportError:
        raise CommandError(
            "--server granian needs the granian extra: "
            "pip install 'domainkit[granian]'."
        ) from None
    uvloop = loop_factory(settings.EVENT_LOOP) is not None
    Granian(
        target,
        address=host,
        port=port,
        interface=Interfaces.ASGI,
        loop=Loops.uvloop if uvloop else Loops.asyncio,
        reload=reload,
        log_dictconfig=copy.deepcopy(dict(settings.LOGGING)),
        log_access=False,
    ).serve()
