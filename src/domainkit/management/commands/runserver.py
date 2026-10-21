"""``runserver``: a development server for ``ASGI_APPLICATION``: uvicorn or Granian."""

from argparse import ArgumentParser
from typing import Final

import uvicorn

from domainkit.conf import settings
from domainkit.management.base import BaseCommand, CommandError
from domainkit.utils.loops import loop_factory

SERVERS: Final = ("uvicorn", "granian")

DEFAULT_ADDRESS = "127.0.0.1"
DEFAULT_PORT = 8000


def parse_address(value: str) -> tuple[str, int]:
    """Parse ``port``, ``host:port``, or ``[ipv6]:port``."""
    host, port = DEFAULT_ADDRESS, value
    if not value.isdigit():
        host, _, port = value.rpartition(":")
        host = host.removeprefix("[").removesuffix("]")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise CommandError(f"{value!r} is not a valid port or address:port.")
    return host, int(port)


class Command(BaseCommand):
    """Run uvicorn, or Granian, with auto-reload; production runs them directly."""

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
            default="uvicorn",
            help="the ASGI server (default: %(default)s; granian needs its extra)",
        )

    def handle(
        self, *, address: str, reload: bool, server: str = "uvicorn", **options: object
    ) -> None:
        """Start the server; framework logging replaces the server's own."""
        if not settings.ASGI_APPLICATION:
            raise CommandError("Set ASGI_APPLICATION, e.g. 'config.asgi:application'.")
        host, port = parse_address(address)
        loop_factory(settings.EVENT_LOOP)  # Refuse a loop that is not installed.
        if server == "granian":
            serve_with_granian(settings.ASGI_APPLICATION, host, port, reload=reload)
            return
        uvicorn.run(
            settings.ASGI_APPLICATION,
            host=host,
            port=port,
            reload=reload,
            loop=settings.EVENT_LOOP,
            log_config=None,
            access_log=False,
            # ProxyHeadersMiddleware and TRUSTED_PROXIES handle forwarding.
            proxy_headers=False,
            server_header=False,
        )


def serve_with_granian(target: str, host: str, port: int, *, reload: bool) -> None:
    """Serve ``target`` (``module:attribute``) with Granian's ASGI interface.

    Granian's own lines go through ``LOGGING``, as the application's do;
    its access log stays off, since ``domainkit.access`` writes one. Its loop
    is ``EVENT_LOOP``'s, resolved here: Granian's own ``auto`` would take
    rloop before uvloop.

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
        log_dictconfig=dict(settings.LOGGING),
        log_access=False,
    ).serve()
