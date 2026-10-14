"""``runworker``: run outbox messages until stopped."""

import asyncio
import signal
from argparse import ArgumentParser

from domainkit.contrib.messaging.worker import Worker
from domainkit.db.routing import router
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Process durable handlers and tasks; SIGTERM finishes running work first.

    One worker runs per outbox (each database that hosts domains has one),
    all in this process unless ``--database`` picks one target.
    """

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
        parser.add_argument(
            "--database",
            help="a target (SQL alias or shard group) or an alias whose outboxes "
            "to run; default: every database that hosts domains",
        )

    async def handle(
        self,
        *,
        once: bool,
        metrics_port: int | None,
        metrics_address: str,
        database: str | None,
        **options: object,
    ) -> None:
        """Run until a signal arrives, or once."""
        if metrics_port is not None:
            try:
                from prometheus_client import start_http_server
            except ImportError as exc:  # pragma: no cover - needs the extra absent.
                raise CommandError("--metrics-port needs domainkit[metrics].") from exc
            start_http_server(metrics_port, addr=metrics_address)
        targets = (database,) if database else router.hosting_targets()
        aliases = dict.fromkeys(
            alias for target in targets for alias in router.aliases_of(target)
        )
        workers = [Worker(database=alias) for alias in aliases]
        if once:
            counts = await asyncio.gather(*(worker.run_once() for worker in workers))
            self.write(f"Processed {sum(counts)} message(s).")
            return
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signal_number, stop.set)
        self.write("Worker started; press Ctrl+C to stop.")
        await asyncio.gather(*(worker.run(stop) for worker in workers))
        self.write("Worker stopped.")
