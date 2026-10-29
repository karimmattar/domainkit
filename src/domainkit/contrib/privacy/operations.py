"""Answering privacy requests, and applying retention, across installed domains.

Each operation is recorded in the audit trail (``privacy.export``,
``privacy.erase``, ``privacy.purge``), attributed to the actor of the
context it runs in (``system`` from a command). An erasure also records
what each domain's ``on_erase_subject`` hooks did to the copies it keeps
(``privacy.copies_erased``).
"""

import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from itertools import groupby
from operator import attrgetter
from typing import Any, Final

from sqlalchemy import (
    ColumnElement,
    ScalarResult,
    and_,
    delete,
    func,
    or_,
    select,
    tuple_,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit import clock
from domainkit.apps.hooks import Hook, on_erase_subject, on_export_subject
from domainkit.contrib.audit import Outcome, forget, record
from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork, connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.errors import sqlstate
from domainkit.utils.decimals import jsonable

from .events import PersonalDataErased
from .models import (
    HOOK,
    PersonalModel,
    Policy,
    personal_models,
    retention_policies,
)

logger = logging.getLogger(__name__)

BATCH_SIZE: Final = 500
"""Rows deleted, or aggregates erased, per transaction by ``purge``."""
REQUEST: Final = "request"
RETENTION: Final = "retention"
_TEXT: Final = re.compile(r"[^\x00-\x1f\x7f]{1,255}")
REFUSALS: Final = {
    "42501": "append-only: the database refuses to delete its rows",
    "23503": "other rows still refer to them",
}
"""SQLSTATEs that stop a purge of one table, and what they mean."""
_NO_MATCH: Final = object()
_KINDS: Final = (uuid.UUID, int)
"""Column types a subject is read as; columns of other types compare it as text."""
_COUNTED: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
"""A kind of copy a domain's ``on_erase_subject`` hook counts: ``deliveries``."""


def check_subject(subject: str) -> str:
    """Return ``subject`` if it can name someone, written as its id prints.

    A subject is 1 to 255 printable characters. Where an installed domain
    names subjects by UUID or integer columns, a subject that reads as one
    must be written as Python prints it (``str(uuid)``, ``str(number)``),
    the form text columns and the audit trail hold: ``3F2A...``,
    ``{3f2a...}``, ``urn:uuid:3f2a...``, ``042`` or ``+42`` is refused,
    so a person is never exported or erased in part.

    Raises:
        ValueError: It cannot; for an id written otherwise, the message
            gives the form to use.
        ImproperlyConfigured: A ``Personal`` marker cannot be followed.
    """
    if not _TEXT.fullmatch(subject):
        raise ValueError("A subject is 1 to 255 characters, without control codes.")
    kinds = {
        _python_type(model, of) for model in personal_models() for of in model.subjects
    }
    for kind in _KINDS:
        value = _parse(kind, subject)
        if kind in kinds and value is not _NO_MATCH and str(value) != subject:
            raise ValueError(f"Write the subject as its id prints: {value}.")
    return subject


def check_reason(reason: str) -> str:
    """Return ``reason`` if it can go into the audit trail: one line, 1 to 255.

    Raises:
        ValueError: It cannot.
    """
    if not _TEXT.fullmatch(reason):
        raise ValueError("A reason is 1 to 255 characters, without control codes.")
    return reason


async def export_subject(subject: str) -> dict[str, Any]:
    """Every field marked ``Personal`` about ``subject``, as JSON-ready data.

    Grouped by domain and model; each record has the aggregate's id and
    its fields about the subject. Under ``copies``, what each domain's
    ``on_export_subject`` hooks give, by its label: the copies it keeps
    outside aggregates (webhook deliveries, kept responses, queued
    arguments). The export is recorded in the audit trail, with how many
    records it holds and which domains gave copies.

    Raises:
        ValueError: ``subject`` is not 1 to 255 printable characters, or is
            an id not written as it prints (see ``check_subject``).
    """
    check_subject(subject)
    domains: dict[str, dict[str, dict[str, dict[str, object]]]] = {}
    for model in personal_models():
        for of, fields in model.subjects.items():
            value = _subject_value(model, of, subject)
            if value is _NO_MATCH:
                continue
            for alias in model.aliases():
                session: AsyncSession
                async with connections.session(alias) as session:
                    rows: ScalarResult[Any] = await session.scalars(
                        select(model.cls).where(model.columns[of] == value)
                    )
                    for row in rows:
                        records = domains.setdefault(model.domain, {}).setdefault(
                            model.model, {}
                        )
                        found = records.setdefault(_identity(row), {})
                        found.update({name: getattr(row, name) for name in fields})
    count = sum(
        len(records) for models in domains.values() for records in models.values()
    )
    copies: dict[str, dict[str, object]] = {}
    for hook in on_export_subject:
        if kept := await hook.call(subject):
            _merge(copies.setdefault(hook.domain, {}), kept)
    await record("privacy.export", target=subject, records=count, copies=sorted(copies))
    return {
        "subject": subject,
        "exported_at": clock.now().isoformat(),
        "domains": jsonable(
            {
                domain: {
                    name: [
                        {"id": key, "fields": fields} for key, fields in rows.items()
                    ]
                    for name, rows in models.items()
                }
                for domain, models in domains.items()
            }
        ),
        "copies": jsonable(copies),
    }


async def erase_subject(subject: str, *, reason: str = REQUEST) -> int:
    """Erase every field marked ``Personal`` about ``subject``; return how many changed.

    Each aggregate is loaded through a unit of work, erased as its model
    says (see ``models``), and committed with a ``PersonalDataErased``
    event, so its version, the audit trail, and event handlers see the
    change. Aggregates already erased are left alone, so running it again
    is harmless. Then each installed domain's ``on_erase_subject`` hooks erase
    the copies it keeps, and what it did is recorded
    (``privacy.copies_erased``, counts only). The subject's keys in the
    audit trail are then forgotten, and the erasure recorded.

    Raises:
        ValueError: ``subject`` or ``reason`` is not 1 to 255 printable
            characters, or ``subject`` is an id not written as it prints
            (see ``check_subject``).
        ImproperlyConfigured: A domain's hook answered something other
            than counts by kind.
    """
    check_subject(subject)
    check_reason(reason)
    erased = 0
    for model in personal_models():
        for of, fields in model.subjects.items():
            value = _subject_value(model, of, subject)
            if value is _NO_MATCH:
                continue
            for alias in model.aliases():
                changed, _ = await _erase(
                    model, alias, model.columns[of] == value, fields, reason
                )
                erased += changed
    for domain, hooks in groupby(on_erase_subject, key=attrgetter("domain")):
        done: dict[str, int] = {}
        for hook in hooks:
            for kind, count in _counts(hook, await hook.call(subject, reason)).items():
                done[kind] = done.get(kind, 0) + count
        if any(done.values()):
            await record(
                "privacy.copies_erased",
                target=subject,
                domain=domain,
                copies=done,
                reason=reason,
            )
        if done.get("pending"):
            logger.warning(
                "%s keeps %d cop%s of a subject's data still to finish (queued "
                "work); they are erased once it ends: erase the subject again then",
                domain,
                done["pending"],
                "y" if done["pending"] == 1 else "ies",
            )
    await forget(subject)
    await record("privacy.erase", target=subject, aggregates=erased, reason=reason)
    return erased


def _merge(copies: dict[str, object], kept: Mapping[str, object]) -> None:
    """Add what one export hook ``kept`` to its domain's ``copies``, losing nothing.

    Two hooks that name the same kind both give copies of it: their values
    are listed together (a list's items, or the value itself), as the
    erasure of that kind sums both hooks' counts.
    """
    for kind, value in kept.items():
        if kind in copies:
            copies[kind] = [*_listed(copies[kind]), *_listed(value)]
        else:
            copies[kind] = value


def _listed(value: object) -> list[object]:
    """``value``'s items if it is a list, else ``value`` alone."""
    return list(value) if isinstance(value, list) else [value]


def _name(call: object) -> str:
    """How an error names a hook's function."""
    return getattr(call, "__qualname__", None) or repr(call)


def _counts(hook: Hook[Any], done: Mapping[str, int]) -> dict[str, int]:
    """``done``, if it is counts by kind: what may go into the audit trail.

    Raises:
        ImproperlyConfigured: It is not: a value that is not a count could
            be the data itself.
    """
    for kind, count in done.items():
        if not (
            isinstance(kind, str)
            and _COUNTED.fullmatch(kind)
            and type(count) is int
            and count >= 0
        ):
            raise ImproperlyConfigured(
                f"{hook.domain}'s erase_subject hook {_name(hook.call)} must "
                "return counts by kind, such as {'deliveries': 2}: lower-case "
                "names and whole numbers, which are audited."
            )
    return dict(done)


class _Aggregates(SqlAlchemyRepository[Any, Any]):
    """Aggregates of one model, loaded for erasure."""

    def __init__(self, session: AsyncSession, model: type) -> None:
        """Load ``model`` aggregates in ``session``."""
        super().__init__(session)
        self.model = model

    async def matching(
        self, condition: ColumnElement[bool], limit: int | None = None
    ) -> list[Any]:
        """The aggregates ``condition`` selects (at most ``limit``), tracked."""
        query = select(self.model).where(condition)
        if limit is not None:
            query = query.limit(limit)
        found = list(await self.session.scalars(query))
        for aggregate in found:
            self._track(aggregate)
        return found


class _ErasureUnitOfWork(SqlAlchemyUnitOfWork):
    """A unit of work over one model's aggregates, on one database."""

    def __init__(self, model: type, alias: str) -> None:
        """Work on ``alias``."""
        self.model = model
        super().__init__(database=alias)

    def create_repositories(self, session: AsyncSession) -> None:
        """One repository: the model's aggregates."""
        self.aggregates = _Aggregates(session, self.model)


async def _erase(
    model: PersonalModel,
    alias: str,
    condition: ColumnElement[bool],
    fields: Sequence[str],
    reason: str,
    limit: int | None = None,
) -> tuple[int, int]:
    """Erase ``fields`` of the aggregates ``condition`` selects, in one commit.

    Returns how many changed, and how many were loaded.
    """
    async with _ErasureUnitOfWork(model.cls, alias) as uow:
        aggregates = await uow.aggregates.matching(condition, limit)
        changed = 0
        for aggregate in aggregates:
            names = _erase_fields(model, aggregate, fields)
            if names:
                aggregate.record(
                    PersonalDataErased(
                        model=model.key,
                        aggregate_id=_identity(aggregate),
                        fields=names,
                        reason=reason,
                    )
                )
                changed += 1
        await uow.commit()
    return changed, len(aggregates)


def _erase_fields(
    model: PersonalModel, aggregate: object, fields: Sequence[str]
) -> tuple[str, ...]:
    """Erase ``fields`` of ``aggregate``; the names it changed (all, for a hook)."""
    if model.erases_itself():
        getattr(aggregate, HOOK)(frozenset(fields))
        return tuple(fields)
    changed = []
    for name in fields:
        erasure = model.erasures[name]
        if not erasure.erased(getattr(aggregate, name)):
            setattr(aggregate, name, erasure.value())
            changed.append(name)
    return tuple(changed)


def _subject_value(model: PersonalModel, of: str, subject: str) -> object:
    """``subject`` as the type of the ``of`` column.

    ``_NO_MATCH`` if it is not one, or is not written as one prints: the
    columns of one person must all match the same text.
    """
    kind = _python_type(model, of)
    if kind not in _KINDS:
        return subject
    value = _parse(kind, subject)
    return value if str(value) == subject else _NO_MATCH  # _NO_MATCH either way.


def _parse(kind: type, subject: str) -> object:
    """``subject`` read as a ``kind`` (a UUID or an integer), else ``_NO_MATCH``."""
    try:
        return kind(subject)
    except ValueError:
        return _NO_MATCH


def _python_type(model: PersonalModel, name: str) -> object:
    """The Python type of a column, or None when SQLAlchemy cannot tell."""
    try:
        return model.columns[name].type.python_type
    except NotImplementedError:
        return None


def _identity(aggregate: Any) -> str:  # noqa: ANN401 - any aggregate.
    """An aggregate's id, as text."""
    return str(aggregate.id)


@dataclass(frozen=True, slots=True)
class Purged:
    """What ``purge`` did to one table on one database."""

    policy: str
    """``<domain>.<table>``."""
    database: str
    action: str
    """``deleted`` or ``erased`` (``would delete``, ``would erase`` in a dry run)."""
    count: int
    before: datetime | date
    refused: str | None = None
    """Why the database stopped the purge, if it did."""


class _Progress:
    """A count that survives the exception that ends the work."""

    def __init__(self) -> None:
        """Count from zero."""
        self.count = 0


async def purge(*, dry_run: bool = False, now: datetime | None = None) -> list[Purged]:
    """Apply every domain's ``RETENTION``, on every database it uses.

    Old rows are deleted, or their aggregates' personal fields erased, in
    transactions of ``BATCH_SIZE``, so a purge never holds a table long. A
    table the database refuses to purge (append-only, or still referenced)
    is reported and recorded, and the others go on. ``dry_run`` only
    counts.
    """
    moment = now or clock.now()
    results: list[Purged] = []
    for policy in retention_policies():
        cutoff = policy.cutoff(moment)
        action = "deleted" if policy.model is None else "erased"
        for alias in policy.aliases():
            if dry_run:
                count = await _count(policy, alias, cutoff)
                results.append(
                    Purged(policy.name, alias, f"would {action[:-1]}", count, cutoff)
                )
                continue
            progress = _Progress()
            try:
                if policy.model is None:
                    await _delete_old(policy, alias, cutoff, progress)
                else:
                    await _erase_old(policy.model, policy, alias, cutoff, progress)
            except DBAPIError as exc:
                refused = REFUSALS.get(sqlstate(exc) or "")
                if refused is None:
                    raise
                results.append(
                    Purged(policy.name, alias, action, progress.count, cutoff, refused)
                )
                await record(
                    "privacy.purge",
                    Outcome.FAILURE,
                    target=policy.name,
                    database=alias,
                    before=cutoff.isoformat(),
                    operation=action,
                    rows=progress.count,
                    reason=refused,
                )
                continue
            results.append(Purged(policy.name, alias, action, progress.count, cutoff))
            await record(
                "privacy.purge",
                target=policy.name,
                database=alias,
                before=cutoff.isoformat(),
                operation=action,
                rows=progress.count,
            )
    return results


def _old(policy: Policy, cutoff: datetime | date) -> ColumnElement[bool]:
    """The rows ``policy`` no longer keeps: older, and for ``erase``, not erased."""
    condition = policy.column < cutoff
    if policy.model is None:
        return condition
    columns = [policy.model.columns[name] for name in policy.model.fields]
    return and_(condition, or_(*(column.is_not(None) for column in columns)))


async def _count(policy: Policy, alias: str, cutoff: datetime | date) -> int:
    """How many rows a purge would delete or erase."""
    query = select(func.count()).select_from(policy.table).where(_old(policy, cutoff))
    async with connections.engine(alias).connect() as connection:
        return int((await connection.execute(query)).scalar_one())


async def _delete_old(
    policy: Policy, alias: str, cutoff: datetime | date, progress: _Progress
) -> None:
    """Delete old rows, a batch per transaction, by primary key."""
    keys = list(policy.table.primary_key.columns)
    while True:
        batch = (
            select(*keys).where(_old(policy, cutoff)).order_by(*keys).limit(BATCH_SIZE)
        )
        async with connections.engine(alias).begin() as connection:
            result = await connection.execute(
                delete(policy.table).where(tuple_(*keys).in_(batch))
            )
        progress.count += result.rowcount
        if result.rowcount < BATCH_SIZE:
            return


async def _erase_old(
    model: PersonalModel,
    policy: Policy,
    alias: str,
    cutoff: datetime | date,
    progress: _Progress,
) -> None:
    """Erase old aggregates' personal fields, a batch per unit of work."""
    while True:
        changed, loaded = await _erase(
            model, alias, _old(policy, cutoff), model.fields, RETENTION, BATCH_SIZE
        )
        progress.count += changed
        if loaded < BATCH_SIZE or not changed:
            return
