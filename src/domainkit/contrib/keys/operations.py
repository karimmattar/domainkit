"""The key ring from ``KEYS``, and what operators do with it.

- ``ring_for(config)``: the ring the settings describe;
- ``shred_subject``: destroy a person's keys, which ``erase_subject``
  does through the ``erase_subject`` hook (crypto-shredding);
- ``key_status``: which key seals how many values, in every column;
- ``reencrypt``: move every value, blind index, and subject's key to the
  current data key, in batches.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

from sqlalchemy import (
    Column,
    ColumnElement,
    Connection,
    Row,
    Table,
    Text,
    and_,
    case,
    func,
    not_,
    or_,
    select,
    tuple_,
    type_coerce,
    update,
)

from domainkit.db import connections
from domainkit.db.encrypted import (
    Ciphertext,
    EncryptedType,
    context,
    encrypted_columns,
    index_context,
)
from domainkit.db.fixtures import tables_of
from domainkit.db.routing import router
from domainkit.encryption import DecryptionFailed
from domainkit.keys import KeyProvider, LocalKeyProvider
from domainkit.keys.ring import DATA, FORMAT, SUBJECT, Keyring, forget_held, sealed_key
from domainkit.signing import derive_keys

from .settings import DEVELOPMENT_KEY, KeysSettings
from .store import TableSubjectKeyStore, clear_indexes, erase
from .tables import subject_keys

BATCH_SIZE: Final = 500
"""Rows moved to the current key per transaction by ``reencrypt``."""
_KINDS: Final = (uuid.UUID, int)
"""Column types a subject is read as; columns of other types compare it as text."""


def provider_for(config: KeysSettings) -> KeyProvider:
    """The key provider ``KEYS.PROVIDER`` names, configured."""
    if config.PROVIDER == "aws-kms":
        from domainkit.keys.aws import AwsKmsKeyProvider

        aws = config.AWS_KMS
        assert aws is not None  # noqa: S101 - the settings check it.
        return AwsKmsKeyProvider(
            aws.KEY_ID, region=aws.REGION, endpoint=aws.ENDPOINT, timeout=config.TIMEOUT
        )
    if config.PROVIDER == "vault":
        from domainkit.keys.vault import VaultTransitKeyProvider

        vault = config.VAULT
        assert vault is not None  # noqa: S101 - the settings check it.
        return VaultTransitKeyProvider(
            vault.URL,
            vault.KEY,
            token=vault.TOKEN.get_secret_value(),
            mount=vault.MOUNT,
            namespace=vault.NAMESPACE,
            timeout=config.TIMEOUT,
        )
    return LocalKeyProvider.from_secret_key()


def ring_for(config: KeysSettings) -> Keyring:
    """The ring ``KEYS`` describes, its subjects' keys in their table.

    The development key ``dev`` is derived from ``SECRET_KEY``, and from
    each of ``SECRET_KEY_FALLBACKS`` to open what it sealed before a
    rotation (SEC-15a-4). It is the ring's one key with the local provider
    and no ``DATA_KEYS``, and stays beside them, retired, while
    ``KEEP_DEVELOPMENT_KEY`` says (with the local provider, by default).
    """
    known: dict[str, bytes] = {}
    alternates: dict[str, tuple[bytes, ...]] = {}
    if config.development_key:
        current, *older = derive_keys("keys.development-data-key")
        known[DEVELOPMENT_KEY] = current
        alternates[DEVELOPMENT_KEY] = tuple(older)
    return Keyring(
        provider_for(config),
        config.DATA_KEYS,
        config.current,
        known=known,
        alternates=alternates,
        subject_keys=TableSubjectKeyStore(),
    )


def hosting_aliases() -> tuple[str, ...]:
    """Every alias that hosts domains, and so holds subjects' keys."""
    aliases = [
        alias
        for target in router.hosting_targets()
        for alias in router.aliases_of(target)
    ]
    return tuple(dict.fromkeys(aliases))


def encrypted_tables(alias: str) -> list[tuple[Table, list[Column[Any]]]]:
    """The tables of ``alias`` with encrypted columns, and those columns."""
    return [
        (table, columns)
        for table in tables_of(alias)
        if (columns := encrypted_columns(table))
    ]


async def shred_subject(subject: str) -> int:
    """Destroy ``subject``'s keys everywhere; return how many there were.

    Values sealed under them can then never be opened: the ORM reads them
    as None. Their ciphertext stays where it is. Their blind indexes are
    cleared, in the rows whose subject column names ``subject``, since a
    blind index outlives the key: whoever holds the data keys could test
    a guessed value against it. A transaction that made the subject's
    first key meanwhile destroys it as it commits (``store``).
    """
    forget_held({subject})
    destroyed = 0
    try:
        for alias in hosting_aliases():
            tables = [table for table, _ in encrypted_tables(alias)]
            async with connections.engine(alias).begin() as connection:
                destroyed += await connection.run_sync(erase, subject)
                await connection.run_sync(clear_indexes, tables, subject)
    finally:
        forget_held({subject})  # Also what a transaction read meanwhile.
    return destroyed


def subject_value(column: Column[Any], subject: str) -> object | None:
    """``subject`` as ``column`` holds it; None if it cannot hold it.

    Values are sealed for the subject their column's value prints as
    (``str(uuid)``, ``str(number)``), so only that form matches.
    """
    try:
        kind = column.type.python_type
    except NotImplementedError:
        kind = str
    for each in _KINDS:
        if isinstance(kind, type) and issubclass(kind, each):
            try:
                value = each(subject)
            except ValueError:
                return None
            return value if str(value) == subject else None
    return subject


@dataclass(frozen=True, slots=True)
class ColumnStatus:
    """Which keys seal the values of one encrypted column of one database."""

    database: str
    table: str
    column: str
    by_key: dict[str, int]
    """Values sealed under each data key, by id."""
    subjects: int
    """Values sealed under subjects' keys."""
    other: int
    """Values sealed under no key the ring holds."""
    stale_indexes: int = 0
    """Blind indexes made under a key other than the current one."""


@dataclass(frozen=True, slots=True)
class KeyStatus:
    """What ``key_status`` found."""

    current: str
    retired: tuple[str, ...]
    columns: list[ColumnStatus] = field(default_factory=list)
    subject_keys: dict[str, dict[str, int]] = field(default_factory=dict)
    """Subjects' keys by database, then by the data key wrapping them."""

    @property
    def on_retired(self) -> int:
        """Values, blind indexes, and subjects' keys still under a retired key."""
        values = sum(
            sum(column.by_key[key_id] for key_id in self.retired) + column.stale_indexes
            for column in self.columns
        )
        keys = sum(
            count
            for counts in self.subject_keys.values()
            for key_id, count in counts.items()
            if key_id != self.current
        )
        return values + keys

    @property
    def unreadable(self) -> int:
        """Values sealed under no key the ring holds."""
        return sum(column.other for column in self.columns)


async def key_status(ring: Keyring) -> KeyStatus:
    """Count, in every hosting database, what each data key seals."""
    status = KeyStatus(
        ring.current, tuple(key for key in ring.ids if key != ring.current)
    )
    for alias in hosting_aliases():
        async with connections.engine(alias).connect() as connection:
            for table, columns in encrypted_tables(alias):
                for column in columns:
                    status.columns.append(
                        await _column_status(connection, alias, table, column, ring)
                    )
            found = await connection.execute(
                select(subject_keys.c.wrapping_key, func.count()).group_by(
                    subject_keys.c.wrapping_key
                )
            )
            status.subject_keys[alias] = {row[0]: row[1] for row in found}
    return status


async def _column_status(
    connection: Any,  # noqa: ANN401 - an AsyncConnection.
    alias: str,
    table: Table,
    column: Column[Any],
    ring: Keyring,
) -> ColumnStatus:
    """One query counting ``column``'s values by the key that sealed them."""
    type_ = cast("EncryptedType", column.type)
    counts = [func.count(column)]
    counts += [
        func.count(case((_sealed_by(column, DATA, key_id), 1))) for key_id in ring.ids
    ]
    counts.append(func.count(case((_sealed_by(column, SUBJECT), 1))))
    if type_.blind_index is not None:
        counts.append(func.count(case((_stale(table.c[type_.blind_index], ring), 1))))
    row = (await connection.execute(select(*counts).select_from(table))).one()
    by_key = dict(zip(ring.ids, row[1 : 1 + len(ring.ids)], strict=True))
    subjects = row[1 + len(ring.ids)]
    stale = row[2 + len(ring.ids)] if type_.blind_index is not None else 0
    return ColumnStatus(
        alias,
        table.name,
        column.name,
        by_key,
        subjects,
        row[0] - sum(by_key.values()) - subjects,
        stale,
    )


def _sealed_by(column: Column[Any], kind: str, key_id: str = "") -> ColumnElement[bool]:
    """Values of ``column`` sealed under a key of ``kind`` (``key_id``, if named)."""
    prefix = f"{FORMAT}:{kind}.{key_id}:" if key_id else f"{FORMAT}:{kind}."
    return type_coerce(column, Text()).startswith(prefix, autoescape=True)


def _stale(index: Column[Any], ring: Keyring) -> ColumnElement[bool]:
    """Blind indexes made under another key than the ring's current one."""
    return and_(
        index.is_not(None), not_(index.startswith(f"{ring.current}:", autoescape=True))
    )


@dataclass(slots=True)
class Reencrypted:
    """What ``reencrypt`` moved to the current key."""

    values: int = 0
    indexes: int = 0
    subject_keys: int = 0
    failed: int = 0
    """Values no key opens: tampered, or sealed under a key the ring lost."""


async def reencrypt(ring: Keyring, *, batch_size: int = BATCH_SIZE) -> Reencrypted:
    """Move everything still under a retired key to the current one.

    In each hosting database: subjects' keys are wrapped again (their
    values stay as they are), values sealed under a retired data key are
    sealed again, and blind indexes made under one are made again. Each
    batch of ``batch_size`` rows commits on its own, so it can stop and
    start again; a row changed meanwhile is left for the next run. A value
    that does not open is counted, never changed.
    """
    done = Reencrypted()
    for alias in hosting_aliases():
        await _rewrap_subject_keys(alias, ring, batch_size, done)
        for table, columns in encrypted_tables(alias):
            for column in columns:
                await _reencrypt_column(alias, table, column, ring, batch_size, done)
    return done


async def _rewrap_subject_keys(
    alias: str, ring: Keyring, batch_size: int, done: Reencrypted
) -> None:
    """Wrap every subject's key of ``alias`` under the current data key."""
    after = ""
    while True:
        async with connections.engine(alias).begin() as connection:
            rows = (
                await connection.execute(
                    select(subject_keys)
                    .where(
                        subject_keys.c.wrapping_key != ring.current,
                        subject_keys.c.id > after,
                    )
                    .order_by(subject_keys.c.id)
                    .limit(batch_size)
                )
            ).all()
            if not rows:
                return
            done.subject_keys += await connection.run_sync(_rewrap_rows, ring, rows)
        after = rows[-1].id


def _rewrap_rows(
    connection: Connection, ring: Keyring, rows: Sequence[Row[Any]]
) -> int:
    """Wrap the keys of ``rows`` again; count those still as read."""
    from domainkit.keys.ring import StoredSubjectKey

    moved = 0
    for row in rows:
        stored = StoredSubjectKey(row.id, row.subject, row.wrapping_key, bytes(row.key))
        try:
            again = ring.wrap_subject_key(
                row.id, row.subject, ring.unwrap_subject_key(stored)
            )
        except DecryptionFailed:
            continue
        result = connection.execute(
            update(subject_keys)
            .where(
                subject_keys.c.id == row.id,
                subject_keys.c.wrapping_key == row.wrapping_key,
            )
            .values(wrapping_key=again.wrapping_key, key=again.wrapped)
        )
        moved += result.rowcount
    return moved


async def _reencrypt_column(
    alias: str,
    table: Table,
    column: Column[Any],
    ring: Keyring,
    batch_size: int,
    done: Reencrypted,
) -> None:
    """Seal ``column``'s values and indexes to move again, in batches in key order."""
    type_ = cast("EncryptedType", column.type)
    index = table.c[type_.blind_index] if type_.blind_index is not None else None
    stale = and_(
        _sealed_by(column, DATA),
        not_(_sealed_by(column, DATA, ring.current)),
    )
    if index is not None:
        stale = or_(stale, _stale(index, ring))
    keys = list(table.primary_key.columns)
    last: tuple[object, ...] | None = None
    while True:
        query = select(*keys, column).where(stale).order_by(*keys).limit(batch_size)
        if last is not None:
            query = query.where(tuple_(*keys) > tuple_(*last))
        async with connections.engine(alias).begin() as connection:
            rows = (await connection.execute(query)).all()
            if not rows:
                return
            await connection.run_sync(
                _reencrypt_rows, ring, table, column, index, rows, done
            )
        last = tuple(rows[-1])[: len(keys)]


def _reencrypt_rows(
    connection: Connection,
    ring: Keyring,
    table: Table,
    column: Column[Any],
    index: Column[Any] | None,
    rows: Sequence[Any],
    done: Reencrypted,
) -> None:
    """Seal each row's value again if a retired key sealed it, and its index."""
    keys = list(table.primary_key.columns)
    for row in rows:
        identity = list(row)[: len(keys)]
        text = row[len(keys)]
        where = context(column, identity)
        try:
            plaintext = ring.open(text, where, connection=connection)
        except DecryptionFailed:
            done.failed += 1
            continue
        values: dict[str, object] = {}
        found = sealed_key(text)
        moved = found is not None and found[0] == DATA and found[1] != ring.current
        if moved and plaintext is not None:
            values[column.name] = Ciphertext(ring.seal(plaintext, where))
        if index is not None:
            values[index.name] = (
                None
                if plaintext is None
                else ring.blind_index(plaintext, index_context(column))
            )
        condition = and_(
            *(key == value for key, value in zip(keys, identity, strict=True)),
            column == Ciphertext(text),
        )
        if connection.execute(update(table).where(condition).values(values)).rowcount:
            done.values += column.name in values
            done.indexes += index is not None
