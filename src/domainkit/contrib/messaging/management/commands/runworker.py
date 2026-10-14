"""``runworker``: run outbox messages until stopped."""

import asyncio
import signal
from argparse import ArgumentParser

from domainkit.apps import registry
from domainkit.contrib.messaging import registry as tasks
from domainkit.contrib.messaging.dispatch import BrokerWorkers
from domainkit.contrib.messaging.settings import MessagingSettings
from domainkit.contrib.messaging.worker import Worker
from domainkit.db.routing import router
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Process durable handlers and tasks; SIGTERM finishes running work first.

    One worker runs per outbox (each database that hosts domains has one),
    all in this process unless ``--database`` picks one target. With
    ``MESSAGING.BROKER``, they relay their outboxes to the broker and run
    what it delivers; ``--once`` always works from the outbox directly.
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
        parser.add_argument(
            "--queue",
            action="append",
            dest="queues",
            metavar="QUEUE",
            help="run only this queue's messages (repeatable); default: every queue",
        )
        parser.add_argument(
            "--no-relay",
            dest="relay",
            action="store_false",
            help="with a broker: only consume, never poll the outbox",
        )

    async def handle(
        self,
        *,
        once: bool,
        metrics_port: int | None,
        metrics_address: str,
        database: str | None,
        queues: list[str] | None,
        relay: bool,
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
        try:
            for queue in queues or ():
                tasks.checked_queue(queue)
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from None
        workers = [Worker(database=alias, queues=queues) for alias in aliases]
        if once:
            counts = await asyncio.gather(*(worker.run_once() for worker in workers))
            self.write(f"Processed {sum(counts)} message(s).")
            return
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signal_number, stop.set)
        config = registry.settings(MessagingSettings)
        if config.BROKER is None and not relay:
            raise CommandError("--no-relay needs MESSAGING.BROKER.")
        if config.BROKER is None:
            self.write("Worker started; press Ctrl+C to stop.")
            await asyncio.gather(*(worker.run(stop) for worker in workers))
        else:
            selected = sorted(queues or tasks.queues())
            self.write(
                f"Worker started on broker {config.BROKER!r} "
                f"(queues: {', '.join(selected)}); press Ctrl+C to stop."
            )
            await BrokerWorkers(workers, selected, config, relay=relay).run(stop)
        self.write("Worker stopped.")
