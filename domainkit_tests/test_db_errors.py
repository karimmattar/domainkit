"""Unique violations become ``DuplicateValue`` (a 409), named by their constraint."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    func,
    insert,
)
from sqlalchemy import UniqueConstraint as Unique
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from domainkit.conf import override_settings
from domainkit.db import connections, create_engine
from domainkit.db.errors import duplicate_value
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.ddd import DuplicateValue
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
    for unrelated in ("23502", "08006", "42P01", "02000"):
        assert refusal(SimpleNamespace(sqlstate=unrelated)) is None, unrelated
    assert refusal(SimpleNamespace(sqlstate=22021)) is None  # Not a SQLSTATE.
    assert refusal(SimpleNamespace()) is None
    pool = database_refusal(PoolTimeout("QueuePool limit reached"))
    assert pool is not None
    assert (pool.status_code, pool.headers) == (503, {"Retry-After": "1"})
    assert database_refusal(ValueError("22021")) is None


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
    assert f"Failing row contains (1, {secret}" in str(shown_error)


def test_errors_without_a_detail_are_left_as_they_are() -> None:
    """Python's own errors and messages that are not text pass through."""
    from domainkit.db.connections import _drop_detail

    plain = ValueError("a message\nDETAIL:  kept, not a database's")
    numbered = OSError(5, "I/O error")
    several = ValueError("first\nDETAIL:  dropped", "second", "third")
    for error in (plain, numbered, several):
        context = SimpleNamespace(sqlalchemy_exception=None, original_exception=error)
        _drop_detail(context)  # type: ignore[arg-type]

    assert plain.args == ("a message",)  # Read as a driver's, it loses its DETAIL.
    assert numbered.args == (5, "I/O error")
    assert several.args == ("first", "second", "third")


def test_network_databases_wait_briefly_for_a_connection(tmp_path: Path) -> None:
    """``POOL_TIMEOUT`` defaults to 10 s for network databases; set, it wins."""
    from domainkit.conf import DatabaseConfig
    from domainkit.db.connections import DEFAULT_POOL_TIMEOUT

    def timeout(**config: object) -> float:
        engine = create_engine(DatabaseConfig.model_validate(config))
        assert engine.sync_engine.pool._pre_ping is True  # Restarts are survived.
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
