"""Who runs the commands that change stored data, and their audit rows."""

import getpass
import io
import logging
import os
from collections.abc import Iterator
from importlib import import_module
from typing import Final

import pytest
from sqlalchemy.exc import DBAPIError

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.context import current_request
from domainkit.db import connections
from domainkit.management import BaseCommand, CommandError, call_command, operators
from domainkit.management.commands.flush import Command as FlushCommand
from domainkit.management.operators import (
    Operator,
    acting_as,
    operator,
    recorded,
)

pytestmark = pytest.mark.anyio

AUDIT = "domainkit.contrib.audit"
ALPHA = "domainkit_tests.sample_domains.alpha"


@pytest.fixture(autouse=True)
def no_operator_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test without ``DOMAINKIT_OPERATOR``."""
    monkeypatch.delenv("DOMAINKIT_OPERATOR", raising=False)


@pytest.fixture
def audit_without_tables(database_url: str) -> Iterator[None]:
    """The audit app installed, its tables never created."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, ALPHA],
        DATABASES={"default": {"URL": database_url}},
    ):
        setup()
        yield
    registry.clear()


def test_the_option_comes_first_then_the_variable_then_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--operator``, else ``DOMAINKIT_OPERATOR``, else the system user."""
    monkeypatch.setattr(getpass, "getuser", lambda: "deploy")

    assert operator() == Operator("deploy", "system")
    monkeypatch.setenv("DOMAINKIT_OPERATOR", "alice@example.com")
    assert operator() == Operator("alice@example.com", "environment")
    assert operator("bob") == Operator("bob", "option")
    assert operator("bob").actor == "operator:bob"
    monkeypatch.setenv("DOMAINKIT_OPERATOR", "")  # Empty is unset.
    assert operator().source == "system"


@pytest.mark.parametrize(
    "name", ["", "-flag", "two words", "a" * 65, "ops\n", "ops:1", "ops/1", "é"]
)
def test_operator_names_are_checked_without_being_quoted(name: str) -> None:
    """A name that could forge or break a row is refused, and never echoed."""
    with pytest.raises(CommandError) as refused:
        operator(name)

    assert str(refused.value) == (
        "The operator from --operator must be 1 to 64 letters, digits, '.', '_', "
        "'@', '+', or '-', starting with a letter or digit; give --operator NAME."
    )


ARBITRARY_UID: Final = 1000870000
"""A uid as OpenShift assigns one: no passwd entry, no name."""


def test_a_bad_variable_is_refused_and_says_where_it_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DOMAINKIT_OPERATOR`` is the operator's own word: a bad one is refused."""
    monkeypatch.setenv("DOMAINKIT_OPERATOR", "a b")
    with pytest.raises(CommandError, match=r"^The operator from DOMAINKIT_OPERATOR"):
        operator()


@pytest.mark.parametrize("login", ["Domain Users\\x", "two words", "", "-x"])
def test_a_login_name_no_operator_could_have_is_the_process_uid(
    login: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A system name ``OPERATOR`` refuses is replaced, never refused nor kept.

    The command runs as ``uid-<n>``, and the source says ``system``: the
    trail shows that nobody gave a name.
    """
    monkeypatch.setattr(getpass, "getuser", lambda: login)
    monkeypatch.setattr(os, "getuid", lambda: ARBITRARY_UID)

    assert operator() == Operator(f"uid-{ARBITRARY_UID}", "system")


def nameless() -> str:
    """``getpass.getuser`` for a uid with no name and no LOGNAME or USER."""
    raise OSError("No username set in the environment")


def test_a_process_without_a_name_runs_as_its_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container under an arbitrary uid runs as it; with no uids, ``unnamed``."""
    monkeypatch.setattr(getpass, "getuser", nameless)
    monkeypatch.setattr(os, "getuid", lambda: ARBITRARY_UID)

    assert operator() == Operator(f"uid-{ARBITRARY_UID}", "system")
    assert operator().actor == f"operator:uid-{ARBITRARY_UID}"
    monkeypatch.delattr(os, "getuid")
    assert operator() == Operator("unnamed", "system")
    assert operator("ops") == Operator("ops", "option")


@pytest.fixture
def nameless_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """No passwd entry for the uid, and none of the variables naming a user."""
    for name in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(getpass, "getuser", nameless)
    monkeypatch.setattr(os, "getuid", lambda: ARBITRARY_UID)


@pytest.mark.usefixtures("auth_project", "nameless_container")
def test_unattended_commands_run_in_a_nameless_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron and entrypoint commands that ran in 0.9.0 still run there.

    ``cleartokens`` from cron, ``createsuperuser --noinput`` from an
    entrypoint, and ``partitions --prune``, whose action is recorded as
    the uid's, from ``system``.
    """
    recorded: list[tuple[str, str, str]] = []

    async def record(
        who: Operator, action: str, outcome: str, details: dict[str, object]
    ) -> None:
        recorded.append((who.actor, who.source, action))

    monkeypatch.setattr(operators, "_record", record)
    out = io.StringIO()

    call_command("cleartokens", stdout=out)
    call_command(
        "createsuperuser",
        "--noinput",
        "--username",
        "ops",
        "--email",
        "ops@example.com",
        stdout=out,
    )
    call_command("partitions", "--prune", stdout=out)

    assert out.getvalue().splitlines()[:2] == [
        "Deleted 0 expired token(s).",
        "Superuser created successfully.",
    ]
    assert recorded == [(f"operator:uid-{ARBITRARY_UID}", "system", "partitions.prune")]


def test_commands_run_as_their_operator() -> None:
    """Inside, the request context names ``operator:<name>``; outside, nothing."""
    with acting_as(Operator("alice", "option"), "flush"):
        context = current_request()

    assert context is not None
    assert (context.actor, context.method, context.path) == (
        "operator:alice",
        "COMMAND",
        "flush",
    )
    assert len(context.request_id) == 32
    assert current_request() is None


def test_only_commands_that_change_data_take_an_operator() -> None:
    """``--operator`` is added where ``operator_actions`` says so."""
    flush = FlushCommand().create_parser("manage.py", "flush").format_help()
    plain = BaseCommand().create_parser("manage.py", "plain").format_help()

    assert "--operator NAME" in flush
    assert "--operator" not in plain
    with pytest.raises(TypeError, match="Unknown options for 'check'"):
        call_command("check", operator="alice", stdout=io.StringIO())


OPERATED: Final = {
    "domainkit.contrib.audit": ["auditforget"],
    "domainkit.contrib.auth": [
        "addcertificate",
        "changepassword",
        "cleartokens",
        "createsuperuser",
        "createtoken",
        "resetmfa",
    ],
    "domainkit.contrib.flags": ["flags"],
    "domainkit.contrib.messaging": ["messages"],
    "domainkit.contrib.privacy": ["privacy"],
    "domainkit.contrib.webhooks": ["webhooks"],
    "domainkit": ["flush", "loaddata", "partitions"],
}
"""The commands that change or erase stored data, or credentials, by package."""


def test_commands_that_change_data_or_credentials_name_their_operator() -> None:
    """Each runs as ``operator:<name>``, so its audit rows say who (SEC-r2-2).

    Those that only read (``evidence``, ``auditlog``, ``auditverify``) do not
    ask for one.
    """
    operated = {
        name
        for package, names in OPERATED.items()
        for name in names
        if import_module(
            f"{package}.management.commands.{name}"
        ).Command.operator_actions
    }
    readers = {
        name
        for package, name in (
            ("domainkit.contrib.evidence", "evidence"),
            ("domainkit.contrib.audit", "auditlog"),
            ("domainkit.contrib.audit", "auditverify"),
        )
        if not import_module(
            f"{package}.management.commands.{name}"
        ).Command.operator_actions
    }

    assert operated == {name for names in OPERATED.values() for name in names}
    assert readers == {"evidence", "auditlog", "auditverify"}


def test_recording_needs_an_operator() -> None:
    """A command without ``operator_actions`` has nobody to record."""
    with pytest.raises(RuntimeError, match="BaseCommand has no operator_actions"):
        BaseCommand().recorded("plain")


async def test_without_the_audit_app_nothing_is_recorded() -> None:
    """The action runs; there is no trail to write to."""
    async with recorded(Operator("alice", "option"), "flush", rows=1) as action:
        action["more"] = 2

    assert action == {"rows": 1, "more": 2}


async def test_a_failed_action_keeps_its_own_error(
    audit_without_tables: None, caplog: pytest.LogCaptureFixture
) -> None:
    """When the record cannot be written either, the action's error is raised.

    The log names the action and the error's class, nothing it could quote.
    """
    with pytest.raises(LookupError, match="the action failed"):
        async with recorded(Operator("alice", "option"), "flush"):
            raise LookupError("the action failed")
    await connections.close_all()

    (logged,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert logged.getMessage() in {  # No table: SQLite's error, or PostgreSQL's.
        "Could not record the failed operator action flush (OperationalError)",
        "Could not record the failed operator action flush (ProgrammingError)",
    }
    assert logged.exc_info is None


async def test_an_action_that_cannot_be_recorded_fails(
    audit_without_tables: None,
) -> None:
    """A successful action whose record is lost is not reported as done."""
    with pytest.raises(DBAPIError):
        async with recorded(Operator("alice", "option"), "flush"):
            pass
    await connections.close_all()


def test_a_refused_operator_stops_the_command_before_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing runs, not even the setup, under a name that is refused."""
    ran: list[str] = []
    monkeypatch.setattr(FlushCommand, "handle", lambda self, **options: ran.append("x"))

    with pytest.raises(CommandError, match=r"^The operator from --operator"):
        call_command("flush", "--operator", "a b", stdout=io.StringIO())

    assert ran == []
