"""Unique violations become ``DuplicateValue`` (a 409), named by their constraint."""

import io
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import asyncpg.exceptions  # type: ignore[import-untyped]
import pytest
from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    event,
    func,
    insert,
    text,
)
from sqlalchemy import UniqueConstraint as Unique
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    IntegrityError,
    InvalidRequestError,
    MissingGreenlet,
    PendingRollbackError,
    ResourceClosedError,
    StatementError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm.exc import FlushError, StaleDataError

from domainkit.conf import DatabaseConfig, override_settings
from domainkit.db import connections, create_engine
from domainkit.db.errors import duplicate_value
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.ddd import ConcurrentUpdate, DuplicateValue
from domainkit.ddd.uow import mark_in_doubt
from domainkit.deadlines import DeadlineExceeded
from domainkit.http.errors import database_refusal, status_code_for
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, test_metadata

pytestmark = pytest.mark.anyio


class EmailTaken(DuplicateValue):
    """Another account has that email."""


class HandleTaken(DuplicateValue):
    """Another account in the tenant has that handle."""


class CodeTaken(DuplicateValue):
    """Another account has that code."""


metadata = MetaData(naming_convention=NAMING_CONVENTION)
accounts = Table(
    "framework_test_accounts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("email", String(50), unique=True, info={"duplicate": EmailTaken}),
    Column("tenant", String(10)),
    Column("handle", String(10)),
    Column("code", String(10)),
    Column("nick", String(10)),
    Column("required", String(10), nullable=False, default="x"),
    Unique("tenant", "handle", info={"duplicate": HandleTaken}),
    Unique("code", info={"duplicate": CodeTaken}),
)
Index("ix_framework_test_accounts_lower_nick", func.lower(accounts.c.nick), unique=True)
strangers = Table(
    "framework_test_strangers",
    MetaData(),
    Column("id", Integer, primary_key=True),
)
"""A table outside the metadata the errors are looked up in."""


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


@pytest.fixture
async def engine(database: str) -> AsyncIterator[AsyncEngine]:
    """An engine on the database, with the test tables created."""
    engine = create_engine(connections.config("default"))
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.run_sync(strangers.metadata.create_all)
    yield engine
    await engine.dispose()


async def refused(engine: AsyncEngine, table: Table, **row: object) -> IntegrityError:
    """Insert ``row`` into ``table`` and return the error it raises."""
    with pytest.raises(IntegrityError) as caught:
        async with engine.begin() as connection:
            await connection.execute(insert(table).values(**row))
    return caught.value


async def test_constraints_name_their_error_and_fields(engine: AsyncEngine) -> None:
    """A column's or a constraint's info picks the class; fields name what is taken.

    The primary key names none, so it is a plain ``DuplicateValue``.
    """
    async with engine.begin() as connection:
        await connection.execute(
            insert(accounts).values(
                id=1, email="a@x", tenant="t", handle="h", code="c", nick="Nick"
            )
        )
    cases: list[tuple[dict[str, object], type[DuplicateValue], str]] = [
        ({"id": 2, "email": "a@x"}, EmailTaken, "uq_framework_test_accounts_email"),
        (
            {"id": 3, "tenant": "t", "handle": "h"},
            HandleTaken,
            "uq_framework_test_accounts_tenant",
        ),
        ({"id": 4, "code": "c"}, CodeTaken, "uq_framework_test_accounts_code"),
        ({"id": 1}, DuplicateValue, "pk_framework_test_accounts"),
    ]
    for row, error_class, constraint in cases:
        error = duplicate_value(await refused(engine, accounts, **row), metadata)
        assert type(error) is error_class
        assert error.constraint == constraint

    taken = duplicate_value(
        await refused(engine, accounts, id=5, tenant="t", handle="h"), metadata
    )
    assert taken is not None
    assert (taken.fields, taken.message) == (
        ("tenant", "handle"),
        "The tenant and handle are already taken.",
    )
    assert taken.code == "handle_taken"
    assert status_code_for(taken) == 409
    email = duplicate_value(
        await refused(engine, accounts, id=6, email="a@x"), metadata
    )
    assert email is not None
    assert (email.fields, email.message) == (("email",), "The email is already taken.")
    assert "a@x" not in email.message


async def test_unique_indexes_on_expressions_are_found_by_name(
    engine: AsyncEngine,
) -> None:
    """SQLite names an index on expressions, not columns; PostgreSQL names it too."""
    async with engine.begin() as connection:
        await connection.execute(insert(accounts).values(id=1, nick="Nick"))

    error = duplicate_value(
        await refused(engine, accounts, id=2, nick="NICK"), metadata
    )

    assert error is not None
    assert error.constraint == "ix_framework_test_accounts_lower_nick"
    assert (error.fields, error.message) == (("nick",), "The nick is already taken.")


async def test_tables_outside_the_metadata_still_get_a_duplicate_value(
    engine: AsyncEngine,
) -> None:
    """The driver's own details are used: columns on SQLite, the name on PostgreSQL."""
    async with engine.begin() as connection:
        await connection.execute(insert(strangers).values(id=1))

    error = duplicate_value(await refused(engine, strangers, id=1), metadata)

    assert type(error) is DuplicateValue
    if engine.dialect.name == "sqlite":
        assert (error.constraint, error.fields) == (None, ("id",))
        assert error.message == "The id is already taken."
    else:
        assert (error.constraint, error.fields) == ("framework_test_strangers_pkey", ())
        assert error.message == "This value is already taken."


async def test_other_integrity_errors_are_left_alone(engine: AsyncEngine) -> None:
    """Only unique violations are duplicates: a missing value is not."""
    error = await refused(engine, accounts, id=1, required=None)

    assert duplicate_value(error, metadata) is None


class AdapterError(Exception):
    """Stands in for the error SQLAlchemy's asyncpg adapter raises."""

    def __init__(self, sqlstate: str, cause: BaseException | None) -> None:
        """Carry ``sqlstate``, raised from ``cause`` (asyncpg's error)."""
        super().__init__("duplicate key value violates unique constraint")
        self.sqlstate = sqlstate
        self.__cause__ = cause


class UniqueViolationError(Exception):
    """Stands in for asyncpg's error, which names the constraint."""

    def __init__(self, constraint_name: str) -> None:
        """Name the violated constraint."""
        super().__init__("duplicate key value violates unique constraint")
        self.constraint_name = constraint_name


def raised(driver_error: object) -> IntegrityError:
    """The IntegrityError SQLAlchemy wraps around ``driver_error``."""
    return IntegrityError("INSERT", {}, driver_error)  # type: ignore[arg-type]


def test_asyncpg_errors_name_their_constraint_on_their_cause() -> None:
    """The adapter's error has the SQLSTATE; asyncpg's, its cause, the name."""
    tenant = UniqueViolationError("uq_framework_test_accounts_tenant")
    known = duplicate_value(raised(AdapterError("23505", tenant)), metadata)
    elsewhere = UniqueViolationError("elsewhere_pkey")
    unknown = duplicate_value(raised(AdapterError("23505", elsewhere)), metadata)
    nameless = duplicate_value(raised(AdapterError("23505", None)), metadata)

    assert type(known) is HandleTaken
    assert known.fields == ("tenant", "handle")
    assert type(unknown) is DuplicateValue  # A table outside the metadata.
    assert (unknown.constraint, unknown.fields, unknown.message) == (
        "elsewhere_pkey",
        (),
        "This value is already taken.",
    )
    assert nameless is not None
    assert (nameless.constraint, nameless.fields) == (None, ())


def test_other_driver_errors_are_left_alone() -> None:
    """Other SQLSTATEs, and errors with neither SQLSTATE nor SQLite name."""
    not_null = raised(AdapterError("23502", None))
    unknown = raised(RuntimeError("connection reset"))

    assert duplicate_value(not_null, metadata) is None
    assert duplicate_value(unknown, metadata) is None


async def test_psycopg_errors_name_their_constraint() -> None:
    """With psycopg, the constraint is in ``diag``, not on a cause."""
    psycopg_error = SimpleNamespace(
        sqlstate="23505",
        diag=SimpleNamespace(constraint_name="uq_framework_test_accounts_email"),
    )
    error = IntegrityError("INSERT", {}, psycopg_error)  # type: ignore[arg-type]

    assert type(duplicate_value(error, metadata)) is EmailTaken


def test_database_refusals_come_from_the_sqlstate() -> None:
    """Busy states, data exceptions, and a pool out of connections; nothing else."""
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    def refusal(driver_error: object) -> tuple[int, str] | None:
        found = database_refusal(DBAPIError("SELECT", {}, driver_error))  # type: ignore[arg-type]
        return None if found is None else (found.status_code, found.code)

    assert refusal(SimpleNamespace(sqlstate="57014")) == (503, "database_busy")
    assert refusal(SimpleNamespace(pgcode="40P01")) == (503, "database_busy")
    assert refusal(SimpleNamespace(sqlstate="22P02")) == (422, "invalid_value")
    assert refusal(SimpleNamespace(pgcode="22003")) == (422, "invalid_value")
    for unrelated in ("23502", "42P01", "02000", "28P01"):
        assert refusal(SimpleNamespace(sqlstate=unrelated)) is None, unrelated
    assert refusal(SimpleNamespace(sqlstate=22021)) is None  # Not a SQLSTATE.
    assert refusal(SimpleNamespace()) is None
    pool = database_refusal(PoolTimeout("QueuePool limit reached"))
    assert pool is not None
    assert (pool.status_code, pool.headers) == (503, {"Retry-After": "1"})
    assert database_refusal(ValueError("22021")) is None


def test_a_database_that_cannot_serve_is_unavailable() -> None:
    """A lost connection, or a server down, starting, or full: 503, longer.

    It is ``database_unavailable``, with a ``Retry-After`` of 5 seconds: a
    restart or a failover takes longer than a busy statement.
    """

    def refusal(driver_error: object, *, invalidated: bool = False) -> object:
        error = DBAPIError(
            "SELECT",
            {},
            driver_error,  # type: ignore[arg-type]
            connection_invalidated=invalidated,
        )
        found = database_refusal(error)
        return None if found is None else (found.status_code, found.code, found.headers)

    unavailable = (503, "database_unavailable", {"Retry-After": "5"})
    for state in ("08006", "08001", "57P01", "57P02", "57P03", "53300"):
        assert refusal(SimpleNamespace(sqlstate=state)) == unavailable, state
    assert refusal(SimpleNamespace(), invalidated=True) == unavailable
    # Lost as it committed: no Retry-After, and the body says it is in doubt.
    lost = DBAPIError("COMMIT", {}, SimpleNamespace(), connection_invalidated=True)  # type: ignore[arg-type]
    kept_nothing = database_refusal(lost)
    mark_in_doubt(lost)
    in_doubt = database_refusal(lost)
    assert kept_nothing is not None
    assert in_doubt is not None
    assert (in_doubt.headers, in_doubt.extensions()) == (None, {"in_doubt": True})
    assert kept_nothing.extensions() == {}
    # Not raised while a connection was opened: a socket's error is not the database's.
    assert database_refusal(ConnectionRefusedError("elsewhere")) is None
    assert database_refusal(DeadlineExceeded("The deadline passed.")) is None


@pytest.mark.usefixtures("database_access")
async def test_a_connection_that_cannot_be_opened_is_unavailable() -> None:
    """The errors asyncpg raises as the pool connects: refused, or starting up.

    A login refused is a mistake in the settings: it is reported as a bug.
    """

    async def failure(make: Any) -> BaseException:  # noqa: ANN401 - an engine's creator.
        engine = create_async_engine(
            "postgresql+asyncpg://postgres@127.0.0.1:1/postgres", async_creator=make
        )
        try:
            with pytest.raises((OSError, DBAPIError)) as caught:
                async with engine.connect():
                    pass  # pragma: no cover - the connection is never made.
        finally:
            await engine.dispose()
        return caught.value

    async def refused() -> None:
        raise ConnectionRefusedError(111, "Connect call failed")

    async def starting() -> None:
        raise asyncpg.exceptions.CannotConnectNowError("the database is starting up")

    async def wrong_password() -> None:
        raise asyncpg.exceptions.InvalidPasswordError("password authentication failed")

    for make in (refused, starting):
        found = database_refusal(await failure(make))
        assert found is not None
        assert (found.status_code, found.code) == (503, "database_unavailable")
    assert database_refusal(await failure(wrong_password)) is None


@pytest.mark.usefixtures("database_access")
@pytest.mark.parametrize("listened", ["checkout", "connect"])
async def test_an_oserror_of_a_pools_listener_is_the_projects_own(
    tmp_path: Path, listened: str
) -> None:
    """A project's listener that fails with an ``OSError`` of its own: a bug.

    It writes an audit file, say, as a connection is handed out or opened:
    a reported 500, not ``database_unavailable``.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'heard.db'}")

    def denied(*args: object) -> None:
        raise PermissionError(13, "Permission denied: '/var/log/audit.log'")

    event.listen(engine.sync_engine, listened, denied)
    try:
        with pytest.raises(PermissionError) as caught:
            async with engine.connect():
                pass  # pragma: no cover - the listener refuses first.
    finally:
        await engine.dispose()
    assert database_refusal(caught.value) is None


async def test_postgres_refusals_are_found_on_real_errors(engine: AsyncEngine) -> None:
    """A NUL is a value PostgreSQL refuses; a statement timeout means busy."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    if engine.dialect.name != "postgresql":
        pytest.skip("SQLite stores a NUL, and has no statement timeout")
    with pytest.raises(DBAPIError) as nul:
        async with engine.begin() as connection:
            await connection.execute(insert(accounts).values(id=1, email="a\x00b"))
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL statement_timeout = 1"))
        with pytest.raises(DBAPIError) as slow:
            await connection.execute(text("SELECT pg_sleep(1)"))
        await connection.rollback()

    invalid, busy = database_refusal(nul.value), database_refusal(slow.value)
    assert invalid is not None
    assert busy is not None
    assert (invalid.code, busy.code) == ("invalid_value", "database_busy")


async def test_row_values_stay_out_of_postgres_errors(engine: AsyncEngine) -> None:
    """``DETAIL: Failing row contains (...)`` is dropped unless ``LOG_PARAMETERS``."""
    from domainkit.conf import DatabaseConfig

    if engine.dialect.name != "postgresql":
        pytest.skip("Only PostgreSQL repeats a row's values in its errors")
    secret = "alice@example.com"
    shown = create_engine(
        DatabaseConfig(URL=connections.config("default").URL, LOG_PARAMETERS=True)
    )
    try:
        hidden_error = await refused(
            engine, accounts, id=1, email=secret, required=None
        )
        shown_error = await refused(shown, accounts, id=1, email=secret, required=None)
    finally:
        await shown.dispose()

    cause = hidden_error.orig.__cause__ if hidden_error.orig else None
    for text_of in (str(hidden_error), str(hidden_error.orig), str(cause)):
        assert "null value in column" in text_of
        assert secret not in text_of
        assert "DETAIL" not in text_of
    # SQLAlchemy 2.1 keeps the driver's DETAIL on its own error too.
    assert getattr(hidden_error.orig, "detail", None) is None
    assert getattr(cause, "detail", None) is None
    shown_cause = shown_error.orig.__cause__ if shown_error.orig else None
    assert f"Failing row contains (1, {secret}" in str(shown_cause)
    assert secret in str(getattr(shown_error.orig, "detail", None))


def test_errors_without_a_detail_are_left_as_they_are() -> None:
    """Python's own errors and messages that are not text pass through."""
    from domainkit.db.connections import _drop_detail

    plain = ValueError("a message\nDETAIL:  kept, not a database's")
    numbered = OSError(5, "I/O error")
    several = ValueError("first\nDETAIL:  dropped", "second", "third")
    empty = ValueError()
    for error in (plain, numbered, several, empty):
        context = SimpleNamespace(sqlalchemy_exception=None, original_exception=error)
        _drop_detail(context)  # type: ignore[arg-type]

    assert plain.args == ("a message",)  # Read as a driver's, it loses its DETAIL.
    assert numbered.args == (5, "I/O error")
    assert several.args == ("first", "second", "third")
    assert empty.args == ()


CARD = "4111111111111111"
"""A value no error text may show unless ``LOG_PARAMETERS``."""


def every_text(error: BaseException) -> str:
    """All an error and its causes show: messages, reprs, a logged traceback."""
    shown = []
    seen: BaseException | None = error
    while seen is not None:
        shown += [str(seen), repr(seen), repr(seen.args)]
        shown += [str(getattr(seen, name, "")) for name in ("detail", "context")]
        seen = seen.__cause__ or seen.__context__
    buffer = io.StringIO()
    log = logging.getLogger("framework_test_db_errors")
    handler = logging.StreamHandler(buffer)
    log.addHandler(handler)
    try:
        log.error("failed", exc_info=error)
    finally:
        log.removeHandler(handler)
    return "\n".join([*shown, buffer.getvalue()])


async def refused_value(
    engine: AsyncEngine, statement: str, value: object
) -> DBAPIError:
    """Run ``statement`` with ``value`` as ``:v``; return the error it raises."""
    with pytest.raises(DBAPIError) as caught:
        async with engine.connect() as connection:
            await connection.execute(text(statement), {"v": value})
    return caught.value


async def test_values_the_driver_or_server_refuses_stay_out_of_errors(
    engine: AsyncEngine,
) -> None:
    """The argument's number and the reason stay; the value does not.

    asyncpg refuses a value it cannot encode, naming it; PostgreSQL quotes
    one it cannot read (an enum's, sent as text), and repeats JSON it
    cannot parse in its context.
    """
    if engine.dialect.driver != "asyncpg":
        pytest.skip("asyncpg's own refusals, and the server's for its types")
    async with engine.begin() as connection:
        await connection.execute(text("DROP TYPE IF EXISTS framework_test_mood"))
        await connection.execute(text("CREATE TYPE framework_test_mood AS ENUM ('ok')"))
    shown = create_engine(
        DatabaseConfig(URL=connections.config("default").URL, LOG_PARAMETERS=True)
    )
    cases = [
        ("SELECT CAST(:v AS integer)", f"{CARD}x"),
        ("SELECT CAST(:v AS float8[])", [f"{CARD}x"]),
        ("SELECT CAST(:v AS framework_test_mood)", f"{CARD}x"),
        ("SELECT CAST(CAST(:v AS text) AS json)", f'{{"card": {CARD}x}}'),
    ]
    try:
        hidden = [await refused_value(engine, *case) for case in cases]
        visible = [await refused_value(shown, *case) for case in cases]
    finally:
        await shown.dispose()

    integer, array, mood, json = hidden
    for error in hidden:
        assert CARD not in every_text(error), error
    for error in visible[:3]:
        assert CARD in str(error)
    assert (
        "invalid input for query argument $1: <hidden> "
        "('str' object cannot be interpreted as an integer)"
    ) in str(integer)
    assert (
        "invalid input for query argument $1: <hidden> "
        "(invalid array element at index 0: must be real number, not str)"
    ) in str(array)
    assert 'invalid input value for enum framework_test_mood: "<hidden>"' in str(mood)
    assert "invalid input syntax for type json" in str(json)
    assert getattr(json.orig, "sqlstate", None) == "22P02"  # Still known.


@pytest.mark.parametrize(
    ("message", "sqlstate", "kept"),
    [
        (
            (
                f"query argument $2 in element #1 of executemany() sequence: "
                f"bad value '{CARD}'"
            ),
            None,
            (
                "query argument $2 in element #1 of executemany() sequence: "
                "bad value '<hidden>'"
            ),
        ),
        (
            (
                f'invalid input syntax for type integer: "{CARD}"\n'
                f"CONTEXT:  unnamed portal parameter $1 = '{CARD}'"
            ),
            "22P02",
            'invalid input syntax for type integer: "<hidden>"',
        ),
        (
            f'value "{CARD}" is out of range for type integer\nHINT:  smaller',
            "22003",
            'value "<hidden>" is out of range for type integer\nHINT:  smaller',
        ),
        (
            (
                f'invalid input syntax for type json\nDETAIL:  Token "{CARD}" is '
                f'invalid.\nCONTEXT:  JSON data, line 1: {{"card": {CARD}...'
            ),
            "22P02",
            "invalid input syntax for type json",
        ),
        (
            (
                f'null value in column "card"\nDETAIL:  Failing row contains '
                f"(1, line\n{CARD}).\nHINT:  fill it"
            ),
            "23502",
            'null value in column "card"\nHINT:  fill it',
        ),
        ('relation "framework_test_cards" does not exist', "42P01", None),
        (
            f"invalid input for query argument $1: '{CARD}' (no reason given)",
            None,
            "invalid input for query argument $1: <hidden>",
        ),
    ],
)
def test_psycopg_messages_lose_their_values(
    message: str, sqlstate: str | None, kept: str | None
) -> None:
    """Messages in psycopg's form keep PostgreSQL's names, never the values.

    psycopg sends values untyped: PostgreSQL refuses them, quoted, and
    repeats them in the DETAIL and CONTEXT of its message.
    """
    from domainkit.db.connections import _drop_detail

    class PsycopgError(Exception):
        """What psycopg raises: the whole message, and the SQLSTATE."""

    driver = PsycopgError(message)
    driver.sqlstate = sqlstate  # type: ignore[attr-defined]
    wrapped = DBAPIError("SELECT :v\n[SQL: SELECT %(v)s]", None, driver)
    context = SimpleNamespace(sqlalchemy_exception=wrapped, original_exception=driver)
    _drop_detail(context)  # type: ignore[arg-type]

    assert driver.args == (kept or message,)
    assert (kept or message) in str(wrapped)
    assert "[SQL: " in str(wrapped)
    assert CARD not in every_text(wrapped)


def test_values_are_masked_in_the_errors_a_refusal_was_raised_from() -> None:
    """Asyncpg raises its refusal from the type's own error, which may quote it.

    Each error down the chain has its quoted values masked; other arguments,
    and errors without a message, stay. A reason that is not the cause's
    goes with the value.
    """
    from domainkit.db.connections import _drop_detail

    def refused(message: str, cause: BaseException) -> Exception:
        refusal = Exception(message)
        refusal.__cause__ = cause
        driver = Exception(message)
        driver.__cause__ = refusal
        context = SimpleNamespace(sqlalchemy_exception=None, original_exception=driver)
        _drop_detail(context)  # type: ignore[arg-type]
        return driver

    empty = ValueError()
    inner = ValueError(f"could not convert string to float: '{CARD}'", "kept")
    inner.__cause__ = empty
    element = TypeError(f"invalid array element at index 0: '{CARD}'")
    element.__cause__ = inner
    array = refused(
        f"invalid input for query argument $1: ['{CARD}'] ({element})", element
    )
    other = refused(
        f"invalid input for query argument $2: '{CARD}' (a reason of its own)",
        ValueError("another reason"),
    )

    assert array.args == (
        (
            "invalid input for query argument $1: <hidden> "
            "(invalid array element at index 0: '<hidden>')"
        ),
    )
    assert element.args == ("invalid array element at index 0: '<hidden>'",)
    assert inner.args == ("could not convert string to float: '<hidden>'", "kept")
    assert empty.args == ()
    assert other.args == ("invalid input for query argument $2: <hidden>",)


def test_network_databases_wait_briefly_for_a_connection(tmp_path: Path) -> None:
    """``POOL_TIMEOUT`` defaults to 10 s for network databases; set, it wins."""
    from domainkit.conf import DatabaseConfig
    from domainkit.db.connections import DEFAULT_POOL_TIMEOUT, _ping_idle

    def timeout(**config: object) -> float:
        engine = create_engine(DatabaseConfig.model_validate(config))
        pool = engine.sync_engine.pool
        tests = pool._pre_ping or any(  # Restarts are survived.
            getattr(listener, "func", None) is _ping_idle
            for listener in pool.dispatch.checkout
        )
        assert tests
        return engine.sync_engine.pool.timeout()  # type: ignore[attr-defined,no-any-return]

    network = "postgresql+asyncpg://app@db/app"
    local = f"sqlite+aiosqlite:///{tmp_path / 'pool.sqlite3'}"
    assert timeout(URL=network) == DEFAULT_POOL_TIMEOUT == 10.0
    assert timeout(URL=network, POOL_TIMEOUT=2.5) == 2.5
    assert timeout(URL=local) == 30.0  # SQLAlchemy's own default.


async def test_a_duplicate_info_must_be_a_duplicate_value_class(
    engine: AsyncEngine,
) -> None:
    """A misnamed class fails loudly instead of being ignored."""
    wrong = MetaData()
    Table(
        "framework_test_strangers",
        wrong,
        Column("id", Integer, info={"duplicate": ValueError}),
        PrimaryKeyConstraint("id", name="framework_test_strangers_pkey"),
    )
    async with engine.begin() as connection:
        await connection.execute(insert(strangers).values(id=1))
    error = await refused(engine, strangers, id=1)

    with pytest.raises(TypeError, match=r"must be a DuplicateValue subclass"):
        duplicate_value(error, wrong)


# The unit of work ---------------------------------------------------------------


@pytest.fixture
async def notes_table(database: str) -> AsyncIterator[None]:
    """The notes table, in the running loop's engine."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(test_metadata.create_all)
    yield
    await connections.close_all()


async def test_a_commit_that_reuses_a_value_raises_duplicate_value(
    notes_table: None,
) -> None:
    """The unique violation at commit is a 409, not a 500."""
    note = Note(id=NoteId(uuid4()), text="first")
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(note)
        await uow.commit()

    async with NotesUnitOfWork() as uow:
        await uow.notes.add(Note(id=note.id, text="again"))
        with pytest.raises(DuplicateValue) as caught:
            await uow.commit()

    assert isinstance(caught.value.__cause__, IntegrityError)
    assert caught.value.code == "duplicate_value"
    assert status_code_for(caught.value) == 409
    assert (uow.committed, uow.in_doubt) == (0, 0)  # Refused: nothing stored.


async def test_a_flush_inside_the_block_raises_duplicate_value(
    notes_table: None,
) -> None:
    """A violation from an earlier flush leaves the block as DuplicateValue."""
    note = Note(id=NoteId(uuid4()), text="first")
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(note)
        await uow.commit()

    async def flush_again() -> None:
        async with NotesUnitOfWork() as uow:
            await uow.notes.add(Note(id=note.id, text="again"))
            await uow.session.flush()

    with pytest.raises(DuplicateValue) as caught:
        await flush_again()

    assert isinstance(caught.value.__cause__, IntegrityError)


async def test_other_errors_leave_the_unit_of_work_unchanged(
    notes_table: None,
) -> None:
    """A missing required value is still an IntegrityError, at commit or before."""
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(Note(id=NoteId(uuid4()), text=None))  # type: ignore[arg-type]
        with pytest.raises(IntegrityError):
            await uow.commit()

    async def flush_without_text() -> None:
        async with NotesUnitOfWork() as uow:
            await uow.notes.add(Note(id=NoteId(uuid4()), text=None))  # type: ignore[arg-type]
            await uow.session.flush()

    async def fail_otherwise() -> None:
        async with NotesUnitOfWork():
            raise RuntimeError("unrelated")

    with pytest.raises(IntegrityError):
        await flush_without_text()
    with pytest.raises(RuntimeError, match=r"^unrelated$"):
        await fail_otherwise()


class FailsToCommit(NotesUnitOfWork):
    """Fails its commit with ``failure``, as the database or the network would."""

    def __init__(self, failure: BaseException) -> None:
        """Fail every commit with ``failure``."""
        super().__init__()
        self.failure = failure

    async def _commit(self) -> None:
        raise self.failure


def answered(sqlstate: str | None, *, invalidated: bool = False) -> DBAPIError:
    """A driver's error with ``sqlstate``, its connection lost or not."""
    return DBAPIError(
        "COMMIT",
        {},
        SimpleNamespace(sqlstate=sqlstate),  # type: ignore[arg-type]
        connection_invalidated=invalidated,
    )


@pytest.mark.parametrize(
    ("failure", "in_doubt"),
    [
        (answered("40001"), 0),  # A lost race: refused.
        (answered("23514"), 0),
        (answered(None), 0),  # SQLite's errors are answers too.
        (StaleDataError("a version moved"), 0),
        # SQLAlchemy's own, before a COMMIT is sent.
        (StatementError("bind failed", "INSERT", {}, ValueError("bad")), 0),
        (FlushError("an instance with that identity is present"), 0),
        (PendingRollbackError("roll back first"), 0),
        (ResourceClosedError("this connection is closed"), 0),
        (PoolTimeout("QueuePool limit reached"), 0),
        (DisconnectionError("the connection was invalid"), 0),
        # Its others may come from an after_commit listener: in doubt.
        (InvalidRequestError("not in a transaction"), 1),
        (MissingGreenlet("greenlet_spawn has not been called"), 1),
        (answered("08006"), 1),  # The connection failed.
        (answered(None, invalidated=True), 1),
        (TimeoutError("command_timeout"), 1),
        (ConnectionResetError("reset"), 1),
    ],
)
async def test_a_commit_the_database_did_not_answer_is_in_doubt(
    notes_table: None, failure: BaseException, in_doubt: int
) -> None:
    """Only a refusal the database sent says nothing was stored."""
    uow = FailsToCommit(failure)

    async def write() -> None:
        async with uow:
            await uow.notes.add(Note(id=NoteId(uuid4()), text="maybe"))
            await uow.commit()

    raised = ConcurrentUpdate if isinstance(failure, StaleDataError) else type(failure)
    with pytest.raises(raised):
        await write()
    assert (uow.committed, uow.in_doubt) == (0, in_doubt)


@pytest.mark.parametrize(
    "failure",
    [InvalidRequestError("not in a transaction"), MissingGreenlet("no greenlet")],
)
async def test_an_error_after_the_commit_landed_is_in_doubt(
    notes_table: None, failure: Exception
) -> None:
    """A session's ``after_commit`` listener fails once the ``COMMIT`` landed.

    The row is stored: the commit counts in doubt, so an upload it names
    is kept, not deleted.
    """
    note = Note(id=NoteId(uuid4()), text="landed")
    uow = NotesUnitOfWork()

    def fails(session: object) -> None:
        raise failure

    async def write() -> None:
        async with uow:
            event.listen(uow.session.sync_session, "after_commit", fails)
            await uow.notes.add(note)
            await uow.commit()

    with pytest.raises(type(failure)):
        await write()
    assert (uow.committed, uow.in_doubt) == (0, 1)
    async with NotesUnitOfWork() as reader:
        assert (await reader.notes.get(note.id)).text == "landed"
