"""``dbshell``: open the database's own client (``psql``, ``sqlite3``)."""

import os
import shutil
import subprocess
from argparse import REMAINDER, ArgumentParser
from pathlib import Path
from typing import Final

from sqlalchemy import URL, make_url

from domainkit.conf import DEFAULT_DB_ALIAS, DatabaseConfig, settings
from domainkit.db.connections import connections
from domainkit.management.base import BaseCommand, CommandError

LIBPQ: Final = {
    "SSLMODE": "PGSSLMODE",
    "SSLROOTCERT": "PGSSLROOTCERT",
    "SSLCERT": "PGSSLCERT",
    "SSLKEY": "PGSSLKEY",
}
"""``DATABASES`` options and the libpq variables that carry them."""
PG_VARIABLES: Final = (
    "PGHOST",
    "PGPORT",
    "PGUSER",
    "PGPASSWORD",
    "PGDATABASE",
    "PGSERVICE",
    "PGPASSFILE",
    *LIBPQ.values(),
)
"""libpq variables replaced for the client, so none leaks in from the shell."""


class Command(BaseCommand):
    """Run ``psql`` or ``sqlite3`` on an alias, as its ``URL``'s user.

    The connection goes to ``psql`` in libpq's environment variables, the
    password in ``PGPASSWORD``: never on the command line, where other
    users of the host can read it. Production refuses without
    ``--allow-production``.
    """

    help = "Open psql or sqlite3 on a database; arguments after -- go to the client."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept an alias, the owner switch, and the client's arguments."""
        parser.add_argument(
            "--database", default=DEFAULT_DB_ALIAS, help="the alias to open"
        )
        parser.add_argument(
            "--owner",
            action="store_true",
            help="connect as the schema owner (MIGRATION_URL)",
        )
        parser.add_argument(
            "--allow-production",
            action="store_true",
            help="required with ENVIRONMENT = 'production'",
        )
        parser.add_argument(
            "parameters",
            nargs=REMAINDER,
            help="after --: arguments for the client (psql -c 'SELECT 1')",
        )

    def handle(
        self,
        *,
        database: str,
        owner: bool,
        allow_production: bool,
        parameters: list[str],
        **options: object,
    ) -> None:
        """Run the client until it exits; fail with its exit status."""
        if settings.ENVIRONMENT == "production" and not allow_production:
            raise CommandError(
                "dbshell gives full access to the production database; repeat "
                "with --allow-production if that is meant."
            )
        config = connections.config(database)
        secret = config.MIGRATION_URL if owner and config.MIGRATION_URL else config.URL
        url = make_url(secret.get_secret_value())
        backend = url.get_backend_name()
        if backend == "postgresql":
            program, arguments, environment = "psql", [], _libpq(url, config)
        elif backend == "sqlite":
            program, arguments, environment = "sqlite3", [_sqlite_file(url)], {}
        else:
            raise CommandError(f"dbshell knows no client for {backend!r} databases.")
        client = shutil.which(program)
        if client is None:
            raise CommandError(f"{program} is not installed, or not on PATH.")
        extra = parameters[1:] if parameters[:1] == ["--"] else parameters
        result = subprocess.run(  # noqa: S603 - the client's path and the operator's arguments.
            [client, *arguments, *extra],
            env={**_without_pg(os.environ), **environment},
            check=False,
        )
        if result.returncode:
            raise CommandError(
                f"{program} exited with {result.returncode}.",
                returncode=result.returncode,
            )


def _libpq(url: URL, config: DatabaseConfig) -> dict[str, str]:
    """The libpq variables for ``url`` and the alias's TLS options."""
    host = url.host or url.query.get("host")
    found = {
        "PGHOST": host if isinstance(host, str) else None,
        "PGPORT": None if url.port is None else str(url.port),
        "PGUSER": url.username,
        "PGPASSWORD": url.password,
        "PGDATABASE": url.database,
    }
    for option, variable in LIBPQ.items():
        value = getattr(config, option)
        found[variable] = None if value is None else str(value)
    return {name: value for name, value in found.items() if value}


def _sqlite_file(url: URL) -> str:
    """The database file of a SQLite URL, as an absolute path."""
    if not url.database or url.database == ":memory:":
        raise CommandError("An in-memory SQLite database has no file to open.")
    return str(Path(url.database).resolve())


def _without_pg(environment: os._Environ[str]) -> dict[str, str]:
    """The environment without libpq's connection variables."""
    return {
        name: value for name, value in environment.items() if name not in PG_VARIABLES
    }
