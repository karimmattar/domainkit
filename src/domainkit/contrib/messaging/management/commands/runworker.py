"""``runworker``: run outbox messages until stopped."""

import asyncio
import signal
from argparse import ArgumentParser, ArgumentTypeError
from contextlib import suppress
from pathlib import Path

from domainkit.apps import registry
from domainkit.contrib.messaging import registry as tasks
from domainkit.contrib.messaging.budget import Budget
from domainkit.contrib.messaging.dispatch import BrokerWorkers
from domainkit.contrib.messaging.heartbeat import heartbeat
from domainkit.contrib.messaging.outbox import outbox_targets
from domainkit.contrib.messaging.settings import MessagingSettings
from domainkit.contrib.messaging.worker import Worker
from domainkit.db.routing import router
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """Process durable handlers and tasks; SIGTERM finishes running work first.

    Running messages get ``MESSAGING.DRAIN_TIMEOUT`` to finish; a second
    signal stops them at once. Either way, stopped messages run again.

    One worker runs per outbox (each database that hosts domains has one),
    all in this process unless ``--database`` picks one target. With
    ``MESSAGING.BROKER``, they relay their outboxes to the broker and run
    what it delivers; ``--once`` always works from the outbox directly.
    ``--heartbeat-file`` rewrites a file while the worker's loop runs, and
    ``--ready-file`` one while its checks hold
    (``domainkit.contrib.messaging.heartbeat``): a container's health check,
    and its readiness.
    Whatever the source, the process runs at most ``MESSAGING.CONCURRENCY``
    messages at once: every outbox and queue draws from one ``Budget``. The
    installed domains start first, as when the application serves
    (``needs_domains``), and stop once the worker has.
    """

    help = "Run the messaging worker (durable event handlers, background tasks)."
    needs_domains = True
    """The work is the application's: its domains start as when it serves
    (a key service's data keys unwrapped first, or ``Encrypted`` fields
    cannot be read)."""

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
        parser.add_argument(
            "--heartbeat-file",
            type=Path,
            metavar="PATH",
            help="rewrite this file while the worker's event loop runs, whatever "
            "its checks find: what a container's health check reads (removed on stop)",
        )
        parser.add_argument(
            "--ready-file",
            type=Path,
            metavar="PATH",
            help="rewrite this file while the worker's checks hold (/ready's and the "
            "broker's), remove it when one fails: its readiness (removed on stop)",
        )
        parser.add_argument(
            "--heartbeat-interval",
            type=_seconds,
            default=15.0,
            metavar="SECONDS",
            help="seconds between beats and checks for --heartbeat-file and "
            "--ready-file (default: %(default)s)",
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
        heartbeat_file: Path | None = None,
        heartbeat_interval: float = 15.0,
        ready_file: Path | None = None,
        **options: object,
    ) -> None:
        """Run until a signal arrives, or once."""
        if metrics_port is not None:
            try:
                import prometheus_client  # noqa: F401 - the extra, first.

                from domainkit.contrib.messaging.metrics import serve
            except ImportError as exc:
                raise CommandError("--metrics-port needs domainkit[metrics].") from exc
            serve(metrics_port, metrics_address)
        targets = (database,) if database else outbox_targets()
        aliases = dict.fromkeys(
            alias for target in targets for alias in router.aliases_of(target)
        )
        try:
            for queue in queues or ():
                tasks.checked_queue(queue)
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from None
        config = registry.settings(MessagingSettings)
        budget = Budget(config.CONCURRENCY)
        workers = [
            Worker(config, database=alias, queues=queues, budget=budget)
            for alias in aliases
        ]
        if once:
            async with asyncio.TaskGroup() as group:
                runs = [group.create_task(worker.run_once()) for worker in workers]
            self.write(f"Processed {sum(run.result() for run in runs)} message(s).")
            return
        if config.BROKER is None and not relay:
            raise CommandError("--no-relay needs MESSAGING.BROKER.")
        stop = asyncio.Event()
        main = asyncio.current_task()
        forced = False

        def on_signal() -> None:
            nonlocal forced
            if stop.is_set() and main is not None:  # The second: stop now.
                forced = True
                main.cancel()
            stop.set()

        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signal_number, on_signal)
        beating = (
            None
            if heartbeat_file is None and ready_file is None
            else asyncio.create_task(
                heartbeat(
                    heartbeat_file,
                    heartbeat_interval,
                    stop,
                    broker=config.BROKER,
                    ready=ready_file,
                ),
                name="domainkit.worker.heartbeat",
            )
        )
        try:
            if config.BROKER is None:
                self.write("Worker started; press Ctrl+C to stop.")
                async with asyncio.TaskGroup() as group:
                    polling = [
                        group.create_task(worker.run(stop)) for worker in workers
                    ]
                if any(run.cancelled() for run in polling):
                    raise asyncio.CancelledError  # Not by a signal: not a stop.
            else:
                selected = sorted(queues or tasks.queues())
                self.write(
                    f"Worker started on broker {config.BROKER!r} "
                    f"(queues: {', '.join(selected)}); press Ctrl+C to stop."
                )
                consumers = BrokerWorkers(
                    workers, selected, config, relay=relay, budget=budget
                )
                await consumers.run(stop)
        except asyncio.CancelledError:
            if not forced or main is None:
                raise
            main.uncancel()
            self.write("Stopped running messages at once; they run again.")
        finally:
            if beating is not None:  # Its files go: a stopped worker is no one's.
                beating.cancel()
                with suppress(asyncio.CancelledError):
                    await beating
        self.write("Worker stopped.")


def _seconds(value: str) -> float:
    """A positive number of seconds, at most an hour (``--heartbeat-interval``)."""
    try:
        seconds = float(value)
    except ValueError:
        raise ArgumentTypeError(f"{value!r} is not a number of seconds") from None
    if not 0 < seconds <= 3600:
        raise ArgumentTypeError("give more than 0 seconds, and at most 3600")
    return seconds
