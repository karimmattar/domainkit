"""``runworker``: run outbox messages until stopped."""

import asyncio
import signal
from argparse import ArgumentParser

from domainkit.contrib.messaging.worker import Worker
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Process durable handlers and tasks; SIGTERM finishes running work first."""

    help = "Run the messaging worker (durable event handlers, background tasks)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept a single-pass switch."""
        parser.add_argument(
            "--once",
            action="store_true",
            help="run the messages that are due now, then exit (cron, tests)",
        )
        parser.add_argument(
            "--metrics-port",
            type=int,
            help="serve Prometheus metrics on this port (needs domainkit[metrics])",
        )
        parser.add_argument(
            "--metrics-address",
            default="127.0.0.1",
            help="address for --metrics-port (default: %(default)s)",
        )

    async def handle(
        self,
        *,
        once: bool,
        metrics_port: int | None,
        metrics_address: str,
        **options: object,
    ) -> None:
        """Run until a signal arrives, or once."""
        if metrics_port is not None:
            try:
                from prometheus_client import start_http_server
            except ImportError as exc:  # pragma: no cover - needs the extra absent.
                raise CommandError("--metrics-port needs domainkit[metrics].") from exc
            start_http_server(metrics_port, addr=metrics_address)
        worker = Worker()
        if once:
            self.write(f"Processed {await worker.run_once()} message(s).")
            return
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signal_number, stop.set)
        self.write("Worker started; press Ctrl+C to stop.")
        await worker.run(stop)
        self.write("Worker stopped.")
