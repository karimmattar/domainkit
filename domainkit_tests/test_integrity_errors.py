"""Integrity errors other than duplicates, and fields named as the API names them."""

import logging
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    event,
    insert,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import registry as orm_registry

from domainkit.conf import override_settings
from domainkit.db import connections, create_engine
from domainkit.db import errors as errors_module
from domainkit.db.errors import (
    IntegrityViolation,
    duplicate_value,
    field_name,
    integrity_violation,
)
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.ddd import DuplicateValue
from domainkit.http import errors as http_errors
from domainkit.http import get_asgi_application
from domainkit.http.errors import (
    database_refusal,
    duplicate_errors,
    integrity_refusal,
    refusal_reason,
)

pytestmark = pytest.mark.anyio

metadata = MetaData(naming_convention=NAMING_CONVENTION)
shelves = Table(
    "framework_test_shelves",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", String(10), unique=True, info={"field": "shelf_code"}),
)
books = Table(
    "framework_test_books",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "shelf",
        ForeignKey("framework_test_shelves.id"),
        info={"field": "shelf_id"},
    ),
    Column("title_text", String(50), nullable=False, default="untitled"),
    Column("pages", Integer),
    Column("isbn", String(20), unique=True),
    CheckConstraint("pages > 0", name="positive_pages"),
)


class Book:
    """A mapped class whose attribute names differ from its columns."""


mapper = orm_registry(metadata=MetaData())
mapper.map_imperatively(
    Book,
    books,
    properties={"title": books.c.title_text},
    exclude_properties=["isbn"],
)


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """The test tables, on an engine that enforces foreign keys on SQLite too."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        engine = create_engine(connections.config("default"))
        if engine.dialect.name == "sqlite":
            event.listen(engine.sync_engine, "connect", _foreign_keys)
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(insert(shelves).values(id=1, code="A"))
            await connection.execute(insert(books).values(id=1, shelf=1, pages=10))
        yield engine
        await engine.dispose()


def _foreign_keys(connection: Any, record: object) -> None:  # noqa: ANN401
    """SQLite checks foreign keys only when asked to."""
    connection.execute("PRAGMA foreign_keys = ON")


async def refused(engine: AsyncEngine, statement: Any) -> IntegrityError:  # noqa: ANN401
    """Run ``statement`` and return the integrity error it raises."""
    with pytest.raises(IntegrityError) as caught:
        async with engine.begin() as connection:
            await connection.execute(statement)
    return caught.value


@pytest.fixture(autouse=True)
def mapped_book(monkeypatch: pytest.MonkeyPatch) -> None:
    """Field names come from the test's own mapper registry."""
    monkeypatch.setattr(errors_module, "mapper_registry", mapper)


async def test_foreign_keys_say_which_side_broke(engine: AsyncEngine) -> None:
    """A missing parent names the field; a parent still referenced names none."""
    postgres = engine.dialect.name == "postgresql"
    missing = integrity_violation(
        await refused(engine, insert(books).values(id=2, shelf=9)), metadata
    )
    referenced = integrity_violation(
        await refused(engine, delete(shelves).where(shelves.c.id == 1)), metadata
    )

    constraint = "fk_framework_test_books_shelf_framework_test_shelves"
    if postgres:
        assert missing == IntegrityViolation(
            "foreign_key", constraint, ("shelf_id",), False
        )
        assert referenced == IntegrityViolation("foreign_key", constraint, (), True)
    else:  # SQLite names neither the constraint nor the side.
        assert missing == referenced == IntegrityViolation("foreign_key")


async def test_not_null_and_check_violations_name_their_fields(
    engine: AsyncEngine,
) -> None:
    """The column's mapped name, and a check constraint's name and columns."""
    missing = integrity_violation(
        await refused(engine, insert(books).values(id=3, title_text=None)), metadata
    )
    negative = integrity_violation(
        await refused(engine, insert(books).values(id=4, pages=-1)), metadata
    )

    assert missing == IntegrityViolation("not_null", None, ("title",))
    assert negative == IntegrityViolation(
        "check", "ck_framework_test_books_positive_pages", ()
    )


async def test_duplicates_use_api_names(engine: AsyncEngine) -> None:
    """``info["field"]`` names a unique column; the duplicate is not a violation."""
    error = await refused(engine, insert(shelves).values(id=2, code="A"))

    duplicate = duplicate_value(error, metadata)
    assert duplicate is not None
    assert duplicate.fields == ("shelf_code",)
    assert duplicate.message == "The shelf_code is already taken."
    assert integrity_violation(error, metadata) is None


class Diag:
    """Stands in for psycopg's ``diag``."""

    def __init__(self, **values: str | None) -> None:
        """Hold the diagnostic fields."""
        self.__dict__.update(values)


def driver(sqlstate: str | None = None, **found: object) -> IntegrityError:
    """An IntegrityError around a driver error with ``sqlstate`` and attributes."""
    error = SimpleNamespace(sqlstate=sqlstate, **found)
    return IntegrityError("INSERT", {}, error)  # type: ignore[arg-type]


class DriverError(Exception):
    """A driver's error, with whatever diagnostics it has as attributes."""

    def __init__(self, **diagnostics: object) -> None:
        """Hold the diagnostics."""
        super().__init__("driver")
        self.__dict__.update(diagnostics)


class AsyncpgError(Exception):
    """Stands in for asyncpg's error: its diagnostics are attributes."""

    def __init__(self, **diagnostics: object) -> None:
        """Hold the diagnostics."""
        super().__init__("asyncpg")
        self.__dict__.update(diagnostics)


def asyncpg(sqlstate: str, **cause: object) -> IntegrityError:
    """SQLAlchemy's asyncpg adapter error, raised from asyncpg's own."""
    adapted = Exception("adapted")
    adapted.sqlstate = sqlstate  # type: ignore[attr-defined]
    adapted.__cause__ = AsyncpgError(**cause)
    return IntegrityError("INSERT", {}, adapted)


def test_postgres_violations_read_psycopg_and_asyncpg() -> None:
    """Both drivers' diagnostics are read; unknown tables give no fields."""
    psycopg = driver(
        "23503",
        diag=Diag(
            constraint_name="fk_framework_test_books_shelf_framework_test_shelves",
            table_name="framework_test_books",
            column_name=None,
            message_primary='insert or update on table "framework_test_books"',
        ),
    )
    by_pgcode = IntegrityError(
        "INSERT",
        {},
        SimpleNamespace(  # type: ignore[arg-type]
            pgcode="23502",
            diag=Diag(table_name="framework_test_books", column_name="pages"),
        ),
    )
    elsewhere = asyncpg("23502", table_name="other_table", column_name="x")
    no_message = asyncpg(
        "23503",
        constraint_name="unknown",
        table_name="framework_test_books",
        message=None,
    )
    unnamed_check = asyncpg("23514", table_name="framework_test_books")

    assert integrity_violation(psycopg, metadata) == IntegrityViolation(
        "foreign_key",
        "fk_framework_test_books_shelf_framework_test_shelves",
        ("shelf_id",),
        False,
    )
    assert integrity_violation(by_pgcode, metadata) == IntegrityViolation(
        "not_null", None, ("pages",)
    )
    assert integrity_violation(elsewhere, metadata) == IntegrityViolation("not_null")
    assert integrity_violation(no_message, metadata) == IntegrityViolation(
        "foreign_key", "unknown", (), False
    )
    assert integrity_violation(unnamed_check, metadata) == IntegrityViolation("check")


def test_sqlite_violations_read_the_message() -> None:
    """The column of NOT NULL, the name of CHECK, nothing of a foreign key."""

    def sqlite(name: str, message: str) -> IntegrityError:
        error = Exception(message)
        error.sqlite_errorname = name  # type: ignore[attr-defined]
        return IntegrityError("INSERT", {}, error)

    unknown_check = sqlite("SQLITE_CONSTRAINT_CHECK", "CHECK constraint failed: gone")
    nameless = sqlite("SQLITE_CONSTRAINT_NOTNULL", "NOT NULL constraint failed: x")

    assert integrity_violation(unknown_check, metadata) == IntegrityViolation(
        "check", "gone"
    )
    assert integrity_violation(nameless, metadata) == IntegrityViolation("not_null")
    for other in (driver("23505"), driver(None), driver(sqlite_errorname=5)):
        assert integrity_violation(other, metadata) is None


def test_check_constraints_on_columns_are_found_with_their_columns() -> None:
    """A constraint written on a column, or with column objects, names them."""
    local = MetaData()
    table = Table(
        "framework_test_ranges",
        local,
        Column("low", Integer, CheckConstraint("low >= 0", name="low_positive")),
        Column("high", Integer),
    )
    table.append_constraint(CheckConstraint(table.c.high > 0, name="high_positive"))

    low = asyncpg("23514", constraint_name="low_positive", table_name=table.name)
    high = asyncpg("23514", constraint_name="high_positive", table_name=table.name)

    assert integrity_violation(low, local) == IntegrityViolation(
        "check", "low_positive"
    )
    assert integrity_violation(high, local) == IntegrityViolation(
        "check", "high_positive", ("high",)
    )


def test_field_names_follow_info_then_mapping_then_column() -> None:
    """``info["field"]`` first, then the mapped attribute, then the column name."""
    assert field_name(books.c.shelf) == "shelf_id"
    assert field_name(books.c.title_text) == "title"
    assert field_name(books.c.isbn) == "isbn"  # Mapped table, unmapped column.
    assert field_name(shelves.c.id) == "id"  # Table nobody maps.


def test_integrity_refusals_answer_with_codes_and_fields() -> None:
    """409 ``referenced``, 422 ``missing_value`` and ``check_violation``."""

    def answer(error: IntegrityError) -> tuple[int, str, str, list[Any] | None]:
        refusal = database_refusal(error)
        assert refusal is not None
        errors = (
            None if refusal.errors is None else [e.model_dump() for e in refusal.errors]
        )
        return refusal.status_code, refusal.code, str(refusal.detail), errors

    missing_parent = asyncpg(
        "23503",
        constraint_name="fk_framework_test_books_shelf_framework_test_shelves",
        table_name="framework_test_books",
        message='insert or update on table "framework_test_books"',
    )
    still_used = asyncpg(
        "23503",
        constraint_name="fk_framework_test_books_shelf_framework_test_shelves",
        table_name="framework_test_books",
        message='update or delete on table "framework_test_shelves"',
    )
    sqlite_key = Exception("FOREIGN KEY constraint failed")
    sqlite_key.sqlite_errorname = "SQLITE_CONSTRAINT_FOREIGNKEY"  # type: ignore[attr-defined]
    not_null = asyncpg("23502", table_name="framework_test_books", column_name="pages")
    check = asyncpg(
        "23514",
        constraint_name="ck_framework_test_books_positive_pages",
        table_name="framework_test_books",
    )

    with _metadata(metadata):
        assert answer(missing_parent) == (
            409,
            "referenced",
            "A record this one refers to does not exist.",
            [
                {
                    "loc": ["body", "shelf_id"],
                    "msg": "No such record.",
                    "type": "referenced",
                }
            ],
        )
        assert answer(still_used)[:3] == (
            409,
            "referenced",
            "Other records still refer to this one; change or remove them first.",
        )
        assert answer(still_used)[3] is None
        assert answer(IntegrityError("DELETE", {}, sqlite_key))[1:] == (
            "referenced",
            (
                "A reference between records would break: a record this one refers "
                "to does not exist, or other records still refer to it."
            ),
            None,
        )
        assert answer(not_null) == (
            422,
            "missing_value",
            "A required value is missing.",
            [
                {
                    "loc": ["body", "pages"],
                    "msg": "This field is required.",
                    "type": "missing_value",
                }
            ],
        )
        assert answer(check) == (
            422,
            "check_violation",
            (
                "A value breaks a rule of the data: it is out of the range or set "
                "the database accepts."
            ),
            None,
        )
        assert integrity_refusal(driver("99999")) is None


def test_duplicates_outside_a_unit_of_work_are_conflicts() -> None:
    """A unique violation raised past the unit of work is still a 409."""
    taken = asyncpg("23505", constraint_name="uq_framework_test_shelves_code")

    with _metadata(metadata):
        refusal = database_refusal(taken)

    assert refusal is not None
    assert (refusal.status_code, refusal.code) == (409, "duplicate_value")
    assert refusal.detail == "The shelf_code is already taken."
    assert [error.loc for error in refusal.errors or []] == [["body", "shelf_code"]]


def test_duplicate_errors_name_each_field() -> None:
    """One error per field; several fields are taken as a combination."""
    single = DuplicateValue("The email is already taken.", fields=["email"])
    several = DuplicateValue("taken", fields=["tenant", "handle"])

    assert duplicate_errors(DuplicateValue("taken")) is None
    assert [e.model_dump() for e in duplicate_errors(single) or []] == [
        {
            "loc": ["body", "email"],
            "msg": "This value is already taken.",
            "type": "duplicate_value",
        }
    ]
    assert {e.msg for e in duplicate_errors(several) or []} == {
        "This combination of values is already taken."
    }


def test_refusal_reasons_name_states_and_constraints() -> None:
    """SQLSTATE or SQLite's name, and the constraint; never a value."""
    from sqlalchemy.exc import DBAPIError

    sqlite_check = Exception(
        "CHECK constraint failed: ck_framework_test_books_positive_pages"
    )
    sqlite_check.sqlite_errorname = "SQLITE_CONSTRAINT_CHECK"  # type: ignore[attr-defined]

    with _metadata(metadata):
        assert refusal_reason(ValueError("x")) == "ValueError"
        busy = DriverError(sqlstate="57014")
        assert refusal_reason(DBAPIError("SELECT", {}, busy)) == "SQLSTATE 57014"
        assert refusal_reason(DBAPIError("SELECT", {}, DriverError())) == (
            "SQLSTATE None"
        )
        assert refusal_reason(IntegrityError("INSERT", {}, sqlite_check)) == (
            "SQLITE_CONSTRAINT_CHECK, constraint ck_framework_test_books_positive_pages"
        )
        assert (
            refusal_reason(
                asyncpg("23505", constraint_name="uq_framework_test_shelves_code")
            )
            == "SQLSTATE 23505, constraint uq_framework_test_shelves_code"
        )
        assert refusal_reason(asyncpg("23502", table_name="x")) == "SQLSTATE 23502"


def _metadata(replacement: MetaData) -> Any:  # noqa: ANN401 - a patcher.
    """Look integrity errors up in ``replacement`` instead of the framework's."""
    return patch.object(http_errors, "metadata", replacement)


router = APIRouter()


@router.post("/books")
async def add_book() -> None:
    """Fail as an insert with a missing NOT NULL value would."""
    raise asyncpg("23502", table_name="framework_test_books", column_name="pages")


@router.post("/duplicate")
async def add_duplicate() -> None:
    """Fail as the unit of work does on a taken value."""
    raise DuplicateValue("The email is already taken.", fields=["email"])


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client of the routes above."""
    with (
        override_settings(ROOT_ROUTER="domainkit_tests.test_integrity_errors.router"),
        _metadata(metadata),
        TestClient(get_asgi_application()) as test_client,
    ):
        yield test_client


def test_integrity_errors_reach_the_client_as_refusals(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A NOT NULL violation is a 422 naming the field, logged as a warning."""
    with caplog.at_level(logging.WARNING, logger="domainkit.http.middleware"):
        response = client.post("/books")

    assert response.status_code == 422
    assert response.json()["code"] == "missing_value"
    assert response.json()["errors"][0]["loc"] == ["body", "pages"]
    assert "Database refused POST /books: missing_value (SQLSTATE 23502)" in caplog.text


def test_duplicates_answer_with_field_errors(client: TestClient) -> None:
    """``DuplicateValue.fields`` reach the body as ``errors[].loc``."""
    response = client.post("/duplicate")

    assert response.status_code == 409
    assert response.json()["errors"] == [
        {
            "loc": ["body", "email"],
            "msg": "This value is already taken.",
            "type": "duplicate_value",
        }
    ]


def ranges() -> tuple[MetaData, Table]:
    """A table whose CHECK constraints name their columns."""
    local = MetaData()
    table = Table(
        "framework_test_ranges",
        local,
        Column("low", Integer, CheckConstraint("low >= 0", name="low_positive")),
        Column("high", Integer),
    )
    table.append_constraint(CheckConstraint(table.c.high > 0, name="high_positive"))
    return local, table


def sqlite_error(name: str, message: str) -> IntegrityError:
    """SQLite's error, its extended name as aiosqlite gives it."""
    error = Exception(message)
    error.sqlite_errorname = name  # type: ignore[attr-defined]
    return IntegrityError("INSERT", {}, error)


def test_sqlite_checks_name_their_columns() -> None:
    """SQLite names the constraint; its table and columns are looked up."""
    local, _ = ranges()
    high = sqlite_error(
        "SQLITE_CONSTRAINT_CHECK", "CHECK constraint failed: high_positive"
    )

    assert integrity_violation(high, local) == IntegrityViolation(
        "check", "high_positive", ("high",)
    )


def test_check_refusals_name_the_fields() -> None:
    """422 ``check_violation``, one error per field the constraint covers."""
    local, table = ranges()
    high = asyncpg("23514", constraint_name="high_positive", table_name=table.name)

    with _metadata(local):
        refusal = database_refusal(high)

    assert refusal is not None
    assert [error.model_dump() for error in refusal.errors or []] == [
        {
            "loc": ["body", "high"],
            "msg": "This value is not allowed.",
            "type": "check_violation",
        }
    ]


def test_diagnostics_may_be_partial() -> None:
    """Unknown tables, missing columns, odd messages, and absent attributes."""
    unknown_table = asyncpg("23514", constraint_name="x", table_name="other_table")
    no_column = asyncpg("23502", table_name="framework_test_books")
    odd_message = asyncpg(
        "23503", constraint_name="x", table_name="framework_test_books", message=5
    )
    still_used = driver(
        "23503",
        diag=Diag(
            constraint_name="fk_framework_test_books_shelf_framework_test_shelves",
            table_name="framework_test_books",
            message_primary='update or delete on table "framework_test_shelves"',
        ),
    )

    assert integrity_violation(unknown_table, metadata) == IntegrityViolation(
        "check", "x"
    )
    assert integrity_violation(no_column, metadata) == IntegrityViolation("not_null")
    assert integrity_violation(odd_message, metadata) == IntegrityViolation(
        "foreign_key", "x", (), False
    )
    assert integrity_violation(still_used, metadata) == IntegrityViolation(
        "foreign_key",
        "fk_framework_test_books_shelf_framework_test_shelves",
        (),
        True,
    )
    for bare in (driver("23505", diag=Diag()), asyncpg("23505")):
        duplicate = duplicate_value(bare, metadata)
        assert duplicate is not None
        assert duplicate.constraint is None


def test_unnamed_unique_constraints_have_no_name() -> None:
    """A constraint nobody named is reported without one."""
    local = MetaData()
    Table(
        "framework_test_codes",
        local,
        Column("id", Integer, primary_key=True),
        Column("code", String(10), unique=True),
    )
    taken = sqlite_error(
        "SQLITE_CONSTRAINT_UNIQUE",
        "UNIQUE constraint failed: framework_test_codes.code",
    )

    duplicate = duplicate_value(taken, local)

    assert duplicate is not None
    assert (duplicate.constraint, duplicate.fields) == (None, ("code",))


def test_field_names_look_at_every_mapper_of_a_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A class that leaves a column out does not hide another that maps it."""
    local = MetaData()
    labels = Table(
        "framework_test_labels",
        local,
        Column("id", Integer, primary_key=True),
        Column("code", String(10)),
    )

    class Plain:
        """Maps the key only."""

    class Labelled:
        """Maps the code as ``label``."""

    mappers = orm_registry(metadata=MetaData())
    mappers.map_imperatively(Plain, labels, exclude_properties=["code"])
    mappers.map_imperatively(Labelled, labels, properties={"label": labels.c.code})
    monkeypatch.setattr(errors_module, "mapper_registry", mappers)

    assert field_name(labels.c.code) == "label"


async def test_tables_with_dots_in_their_names_are_found(database_url: str) -> None:
    """SQLite writes ``table.column``; the column is after the last dot."""
    local = MetaData(naming_convention=NAMING_CONVENTION)
    dotted = Table(
        "framework_test.dotted",
        local,
        Column("id", Integer, primary_key=True),
        Column("code", String(10), nullable=False, unique=True),
    )
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        engine = create_engine(connections.config("default"))
        try:
            async with engine.begin() as connection:
                await connection.run_sync(local.create_all)
                await connection.execute(insert(dotted).values(id=1, code="A"))
            taken = await refused(engine, insert(dotted).values(id=2, code="A"))
            missing = await refused(engine, insert(dotted).values(id=3, code=None))
        finally:
            await engine.dispose()

    duplicate = duplicate_value(taken, local)
    assert duplicate is not None
    assert (duplicate.fields, duplicate.message) == (
        ("code",),
        "The code is already taken.",
    )
    assert integrity_violation(missing, local) == IntegrityViolation(
        "not_null", None, ("code",)
    )
