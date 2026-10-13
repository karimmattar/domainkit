"""Alembic runs this file; settings, not ``alembic.ini``, configure it."""

from domainkit.db.migrations import run_migrations

run_migrations()
