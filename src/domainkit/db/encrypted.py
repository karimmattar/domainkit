"""Columns encrypted at rest: sealed by the ORM, bound to their row.

A field marked ``Encrypted`` (``domainkit.ddd``) is stored in an
``EncryptedType`` column: text that holds the value sealed with
AES-256-GCM under a data key of the process's key ring (``domainkit.keys.ring``,
from ``KEYS``), never the value::

    card = Table(
        "payments_cards", metadata,
        Column("id", Uuid, primary_key=True),
        Column("holder_id", Uuid, nullable=False),
        *EncryptedColumns("number", subject="holder_id", blind_index=True),
        Column("version", Integer, nullable=False),
    )

``table_for`` derives the same columns for ``Annotated[str | None,
Encrypted(subject="holder_id", blind_index=True)]``. ``map_imperatively``
then seals each changed value as its row is written, and opens it as the
row is loaded, so the aggregate holds the value and the database its
ciphertext. A sealed value is bound to its table, column, and row (its
primary key): copied to another row or column it does not open. Its key
is named in it (``dk1:k.<id>:`` or ``dk1:s.<id>:``), so the ring can
change keys while older values stay readable.

With ``subject``, the value is sealed under the key of the person the
``subject`` column names (``Encrypted(subject=...)``); erasing that person
destroys the key, and the value reads as None from then on.

An encrypted column is not queryable by value: its ciphertexts differ for
equal values. A query can ask whether it is None; with ``blind_index``,
whether it equals a value, through the ``<name>_index`` column, which
holds an HMAC of the value under a key derived from the current data key
(``<key id>:<32 hex digits>``). The index leaks which rows hold equal
values, and lets whoever holds the data keys test a guessed value: see
``docs/contrib/keys.md``. Raw SQL, fixtures (``dumpdata``), and backups
see only ciphertext and indexes. See ADR 0120.
"""

import base64
import binascii
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast
from weakref import WeakKeyDictionary

from sqlalchemy import Column, ColumnElement, Index, String, Table, Text, event
from sqlalchemy.engine import Connection, Dialect
from sqlalchemy.orm import Mapper, QueryContext, Session, attributes
from sqlalchemy.orm.attributes import instance_state, set_committed_value
from sqlalchemy.orm.exc import UnmappedColumnError
from sqlalchemy.sql.schema import SchemaItem
from sqlalchemy.types import TypeDecorator

from domainkit.ddd.fields import InvalidQuery, primitive, wrapped_field
from domainkit.exceptions import ImproperlyConfigured
from domainkit.keys.ring import SUBJECT, keyring, sealed_key

__all__ = [
    "Ciphertext",
    "EncryptedColumns",
    "EncryptedType",
    "Plaintext",
    "lookup",
]

INDEX: Final = "blind_index"
"""The ``info`` key of a blind index column: the name of the column it indexes."""
INDEX_SUFFIX: Final = "_index"
INDEX_LENGTH: Final = 72
"""Room for ``<key id, up to 32>:<32 hex digits>``, and some to spare."""
_PENDING: Final = "domainkit.encrypted.pending"
"""The ``Session.info`` key of the values sealed in a flush, until written."""
_FIELDS: WeakKeyDictionary[Mapper[Any], tuple["_Field", ...]] = WeakKeyDictionary()
"""Each mapper's encrypted attributes, read once."""


class Ciphertext(str):
    """A sealed value, as an ``EncryptedType`` column holds it.

    What the column gives back, and the only text it takes: a plain
    string bound to it is refused, so a query cannot compare ciphertext
    with a value by mistake.
    """

    __slots__ = ()


@dataclass(frozen=True, slots=True, repr=False)
class Plaintext:
    """A value a fixture gives in clear (``{"plaintext": ...}``), sealed as it loads."""

    value: object

    def __repr__(self) -> str:
        """Never the value."""
        return "Plaintext(<hidden>)"


class EncryptedType(TypeDecorator[Any]):
    """Text holding a value sealed under the key ring (see the module).

    ``kind`` is the class of the values: ``str``, ``bytes``, or a value
    object with one field of either (or of such an object). ``subject``
    names the column whose value identifies the person the value is
    about; ``blind_index`` names the column of its blind index.
    """

    impl = Text
    cache_ok = True

    def __init__(
        self,
        kind: type = str,
        *,
        subject: str | None = None,
        blind_index: str | None = None,
    ) -> None:
        """Store ``kind`` values, sealed for ``subject``'s key if given.

        Raises:
            TypeError: ``kind`` is not text, bytes, or a value object of one.
        """
        super().__init__()
        self.kind = kind
        self.subject = subject
        self.blind_index = blind_index
        self._chain = _chain(kind)

    def __repr__(self) -> str:
        """``EncryptedType(str, subject='holder_id')``."""
        options = "".join(
            f", {name}={value!r}"
            for name, value in (
                ("subject", self.subject),
                ("blind_index", self.blind_index),
            )
            if value is not None
        )
        return f"EncryptedType({self.kind.__name__}{options})"

    def process_bind_param(self, value: object, dialect: Dialect) -> str | None:
        """The sealed text; a value in clear is refused.

        Raises:
            TypeError: ``value`` is not sealed: the ORM seals what it writes,
                and a query cannot compare a value with ciphertext.
        """
        if value is None:
            return None
        if isinstance(value, Ciphertext):
            return str(value)
        raise TypeError(
            "An encrypted column holds sealed values only: the ORM seals what "
            "an aggregate holds, and a query can compare it only with None, or "
            "by equality through its blind index (domainkit.db.encrypted.lookup)."
        )

    def process_result_value(
        self, value: object, dialect: Dialect
    ) -> Ciphertext | None:
        """The sealed text, marked as such; the ORM opens it on load."""
        return None if value is None else Ciphertext(str(value))

    def to_bytes(self, value: object) -> bytes:
        """The bytes sealed for ``value``: text as UTF-8; a value object's field.

        Raises:
            TypeError: ``value`` is not of the column's kind.
        """
        for cls, field in self._chain[:-1]:
            if isinstance(value, cls):
                value = getattr(value, field)
        if isinstance(value, str) and self._chain[-1][0] is str:
            return value.encode("utf-8", "surrogatepass")
        if isinstance(value, bytes) and self._chain[-1][0] is bytes:
            return value
        raise TypeError(
            f"An encrypted column of {self.kind.__name__} takes "
            f"{self.kind.__name__}, not {type(value).__name__}."
        )

    def from_bytes(self, data: bytes) -> object:
        """The value ``data`` holds, as the column's kind."""
        value: object = (
            data.decode("utf-8", "surrogatepass") if self._chain[-1][0] is str else data
        )
        for cls, _ in reversed(self._chain[:-1]):
            value = cls(value)
        return value

    def to_json(self, value: object) -> object:
        """A value in a fixture: text as it is, bytes as base64."""
        data = self.to_bytes(value)
        if self._chain[-1][0] is str:
            return data.decode("utf-8", "surrogatepass")
        return base64.b64encode(data).decode()

    def from_json(self, value: object) -> object:
        """What ``to_json`` wrote, as the column's kind.

        Raises:
            ValueError: It is not text (and base64, for bytes).
        """
        if not isinstance(value, str):
            raise ValueError("not text")
        if self._chain[-1][0] is str:
            return self.from_bytes(value.encode("utf-8", "surrogatepass"))
        try:
            return self.from_bytes(base64.b64decode(value, validate=True))
        except binascii.Error:
            raise ValueError("not base64") from None


def _chain(kind: type) -> tuple[tuple[type, str], ...]:
    """The value objects ``kind`` wraps, outside in, then ``str`` or ``bytes``.

    Raises:
        TypeError: It ends in neither.
    """
    chain: list[tuple[type, str]] = []
    current = kind
    while (inner := wrapped_field(current)) is not None and isinstance(
        inner.kind, type
    ):
        chain.append((current, inner.name))
        current = inner.kind
    if current not in {str, bytes}:
        raise TypeError(
            f"An encrypted field holds text or bytes (or a value object of "
            f"them), not {kind.__name__}."
        )
    chain.append((current, ""))
    return tuple(chain)


class EncryptedColumns:
    """The column of an encrypted field, and its blind index if it has one.

    Pass them to a ``Table`` (``*EncryptedColumns("number")``): ``<name>``
    holds the sealed value (``EncryptedType``), and with ``blind_index``,
    ``<name>_index`` the value's blind index, indexed.
    """

    def __init__(
        self,
        name: str,
        kind: type = str,
        *,
        subject: str | None = None,
        blind_index: bool = False,
        nullable: bool = True,
    ) -> None:
        """Describe the columns of the encrypted field ``name``."""
        index = f"{name}{INDEX_SUFFIX}" if blind_index else None
        self.value: Column[Any] = Column(
            name,
            EncryptedType(kind, subject=subject, blind_index=index),
            nullable=nullable,
        )
        self.index: Column[str] | None = None
        if index is not None:
            self.index = Column(index, String(INDEX_LENGTH), info={INDEX: name})

    def __iter__(self) -> Iterator[SchemaItem]:
        """The value column, then the index column and its index."""
        yield self.value
        if self.index is not None:
            yield self.index
            yield Index(None, self.index)


def encrypted_columns(table: Table) -> list[Column[Any]]:
    """The encrypted columns of ``table``."""
    return [
        column for column in table.columns if isinstance(column.type, EncryptedType)
    ]


def index_properties(table: Table, given: Mapping[str, Any]) -> dict[str, Any]:
    """The blind index columns of ``table``, mapped to private attributes.

    ``_<column>`` (``_number_index``), unless ``given`` maps one.
    """
    return {
        f"_{column.name}": column
        for column in table.columns
        if INDEX in column.info and f"_{column.name}" not in given
    }


def context(column: Column[Any], identity: Sequence[object]) -> bytes:
    """What a value of ``column`` in the row ``identity`` (its key) is sealed for.

    Each part of the key as it prints (an id object as the id it wraps).
    """
    row = "\x1f".join(str(primitive(part)) for part in identity)
    return f"{column.table.name}\x00{column.name}\x00{row}".encode(
        "utf-8", "surrogatepass"
    )


def index_context(column: Column[Any]) -> bytes:
    """What a blind index of ``column`` is made for: its table and column, no row."""
    return f"{column.table.name}\x00{column.name}".encode()


def lookup(column: Column[Any], values: Sequence[object]) -> ColumnElement[bool]:
    """The rows whose ``column`` equals one of ``values``, through its blind index.

    Each value is hashed under every data key of the ring, so rows not yet
    moved to the current key still match.

    Raises:
        InvalidQuery: ``column`` has no blind index.
    """
    type_ = cast("EncryptedType", column.type)
    if type_.blind_index is None:
        raise InvalidQuery(
            f"{column.table.name}.{column.name} is encrypted without a blind "
            "index: a query can only ask whether it is None."
        )
    ring = keyring()
    where = index_context(column)
    hashes = [
        index
        for value in values
        for index in ring.blind_indexes(type_.to_bytes(value), where)
    ]
    return column.table.c[type_.blind_index].in_(hashes)


def seal_row(
    connection: Connection, table: Table, row: Mapping[str, Any]
) -> dict[str, Any]:
    """``row`` of ``table``, its ``Plaintext`` values sealed and their indexes made.

    For rows written without the ORM, as ``loaddata`` writes a fixture's
    values given in clear: each is bound to the row's primary key, and
    sealed for the subject its column names.

    Raises:
        ImproperlyConfigured: The row lacks part of its primary key.
    """
    sealed = dict(row)
    for column in encrypted_columns(table):
        value = sealed.get(column.name)
        if not isinstance(value, Plaintext):
            continue
        type_ = cast("EncryptedType", column.type)
        identity = [sealed.get(key.name) for key in table.primary_key.columns]
        if any(part is None for part in identity):
            raise ImproperlyConfigured(
                f"{table.name}.{column.name} is bound to its row's primary key, "
                "which this row lacks."
            )
        index = None
        if value.value is None:
            sealed[column.name] = None
        else:
            ring = keyring()
            data = type_.to_bytes(value.value)
            subject = sealed.get(type_.subject) if type_.subject else None
            sealed[column.name] = Ciphertext(
                ring.seal(
                    data,
                    context(column, identity),
                    connection=connection,
                    subject=None if subject is None else str(primitive(subject)),
                )
            )
            index = ring.blind_index(data, index_context(column))
        if type_.blind_index is not None:
            sealed[type_.blind_index] = index
    return sealed


def open_row(
    connection: Connection, table: Table, row: Mapping[str, Any]
) -> dict[str, Any]:
    """The values of ``row``'s encrypted columns, opened: None where shredded.

    For reads without the ORM, as ``dumpdata --decrypt`` reads.

    Raises:
        DecryptionFailed: A value does not open.
    """
    opened: dict[str, Any] = {}
    ring = keyring()
    for column in encrypted_columns(table):
        value = row.get(column.name)
        if value is None:
            opened[column.name] = None
            continue
        identity = [row[key.name] for key in table.primary_key.columns]
        data = ring.open(str(value), context(column, identity), connection=connection)
        type_ = cast("EncryptedType", column.type)
        opened[column.name] = None if data is None else type_.from_bytes(data)
    return opened


@dataclass(frozen=True, slots=True)
class _Field:
    """One encrypted attribute of a mapped class, and the attributes it depends on."""

    key: str
    column: Column[Any]
    type: EncryptedType
    subject: str | None
    """The attribute naming the subject, if sealed for one."""
    index: str | None
    """The attribute holding the blind index, if there is one."""


def watch(class_: type, table: Table) -> None:
    """Seal and open ``class_``'s encrypted attributes as its rows are written, read."""
    if not encrypted_columns(table):
        return
    event.listen(class_, "before_insert", _seal)
    event.listen(class_, "before_update", _seal)
    event.listen(class_, "after_insert", _restore)
    event.listen(class_, "after_update", _restore)
    event.listen(class_, "load", _open)
    event.listen(class_, "refresh", _refreshed)


def _fields(mapper: Mapper[Any]) -> tuple[_Field, ...]:
    """The encrypted attributes of ``mapper``, read once.

    Raises:
        ImproperlyConfigured: A subject or index column is not mapped.
    """
    if (found := _FIELDS.get(mapper)) is not None:
        return found
    fields = []
    for column in encrypted_columns(cast("Table", mapper.local_table)):
        type_ = cast("EncryptedType", column.type)
        fields.append(
            _Field(
                mapper.get_property_by_column(column).key,
                column,
                type_,
                _attribute(mapper, column, type_.subject),
                _attribute(mapper, column, type_.blind_index),
            )
        )
    _FIELDS[mapper] = found = tuple(fields)
    return found


def _attribute(
    mapper: Mapper[Any], column: Column[Any], name: str | None
) -> str | None:
    """The attribute the column ``name`` of ``column``'s table is mapped to.

    Raises:
        ImproperlyConfigured: The table has no such column, or it is not mapped.
    """
    if name is None:
        return None
    table = column.table
    try:
        return mapper.get_property_by_column(table.c[name]).key
    except KeyError:
        raise ImproperlyConfigured(
            f"{table.name}.{column.name} names the column {name!r}, which "
            f"{table.name} does not have."
        ) from None
    except UnmappedColumnError as exc:
        raise ImproperlyConfigured(
            f"{table.name}.{column.name} names the column {name!r}, which "
            f"{mapper.class_.__name__} does not map."
        ) from exc


def _identity(mapper: Mapper[Any], target: object) -> list[object]:
    """The primary key of ``target``.

    Raises:
        ImproperlyConfigured: Part of it is missing: an encrypted value is
            bound to its row, so the row needs its key before it is written.
    """
    identity = list(mapper.primary_key_from_instance(target))
    if any(part is None for part in identity):
        raise ImproperlyConfigured(
            f"{mapper.class_.__name__} has encrypted fields, which are bound to "
            "the row's primary key: give it its id before saving it, rather than "
            "letting the database number it."
        )
    return identity


def _changed(target: object, field: _Field, mapper: Mapper[Any]) -> bool:
    """Whether the value, its subject, or the row's key changed since loaded."""
    keys = [field.key, *([field.subject] if field.subject else [])]
    keys += [mapper.get_property_by_column(column).key for column in mapper.primary_key]
    return any(attributes.get_history(target, key).has_changes() for key in keys)


def _seal(mapper: Mapper[Any], connection: Connection, target: object) -> None:
    """Seal each new or changed value of ``target`` before its row is written.

    The attribute holds the ``Ciphertext`` while the statement runs, and
    the value again once it ran (``_restore``), or the flush failed.
    """
    state = instance_state(target)
    session = cast("Session", state.session)  # Flushing: it has one.
    ring = None
    for field in _fields(mapper):
        if state.has_identity and not _changed(target, field, mapper):
            continue
        value = state.dict.get(field.key)
        if isinstance(value, Ciphertext):
            continue
        index = None
        if value is not None:
            ring = ring or keyring()
            data = field.type.to_bytes(value)
            subject = getattr(target, field.subject) if field.subject else None
            sealed = ring.seal(
                data,
                context(field.column, _identity(mapper, target)),
                connection=connection,
                subject=None if subject is None else str(primitive(subject)),
            )
            session.info.setdefault(_PENDING, []).append((target, field.key, value))
            setattr(target, field.key, Ciphertext(sealed))
            if field.index is not None:
                index = ring.blind_index(data, index_context(field.column))
        if field.index is not None:
            setattr(target, field.index, index)


def _restore(mapper: Mapper[Any], connection: Connection, target: object) -> None:
    """Give ``target`` its values back once its row is written."""
    session = cast("Session", instance_state(target).session)  # Just flushed in it.
    pending = session.info.get(_PENDING, [])
    for entry in [entry for entry in pending if entry[0] is target]:
        pending.remove(entry)
        set_committed_value(target, entry[1], entry[2])


@event.listens_for(Session, "after_soft_rollback")
def _rolled_back(session: Session, previous_transaction: object) -> None:
    """Give back the values a failed flush left sealed in their attributes."""
    for target, key, value in session.info.pop(_PENDING, []):
        set_committed_value(target, key, value)


def _open(target: object, context_: QueryContext) -> None:
    """Open each sealed value of a row just loaded."""
    state = instance_state(target)
    mapper = state.mapper
    session = context_.session
    for field in _fields(mapper):
        value = state.dict.get(field.key)
        if not isinstance(value, Ciphertext):
            continue
        sealed = sealed_key(value)
        connection = (
            session.connection(bind_arguments={"mapper": mapper})
            if sealed is not None and sealed[0] == SUBJECT
            else None
        )
        opened = keyring().open(
            value,
            context(field.column, _identity(mapper, target)),
            connection=connection,
        )
        set_committed_value(
            target, field.key, None if opened is None else field.type.from_bytes(opened)
        )


def _refreshed(target: object, context_: QueryContext, attrs: object) -> None:
    """Open the sealed values a refresh loaded."""
    _open(target, context_)
