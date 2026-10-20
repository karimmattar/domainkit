"""``runserver``: serve ``ASGI_APPLICATION`` with uvicorn for development."""

from argparse import ArgumentParser

import uvicorn

from domainkit.conf import settings
from domainkit.management.base import BaseCommand, CommandError
from domainkit.utils.loops import loop_factory

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
    """Run uvicorn with auto-reload; use a process manager in production."""

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

    def handle(self, *, address: str, reload: bool, **options: object) -> None:
        """Start uvicorn; framework logging replaces uvicorn's own."""
        if not settings.ASGI_APPLICATION:
            raise CommandError("Set ASGI_APPLICATION, e.g. 'config.asgi:application'.")
        host, port = parse_address(address)
        loop_factory(settings.EVENT_LOOP)  # Refuse a loop that is not installed.
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
