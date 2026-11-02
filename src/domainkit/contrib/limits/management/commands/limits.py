"""``limits``: list limits, show a subject's usage, set a limit, purge old usage."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

from sqlalchemy import select

from domainkit.contrib.limits.models import Direction, Window
from domainkit.contrib.limits.services import SetLimit, set_limit
from domainkit.contrib.limits.tables import limits
from domainkit.contrib.limits.uow import LimitsUnitOfWork, limits_alias
from domainkit.contrib.limits.usage import (
    Rule,
    Usage,
    config,
    default_rule,
    purge_usage,
    stored_rule,
    usage_of,
)
from domainkit.db import connections
from domainkit.ddd import DomainError
from domainkit.management import ActionCommand, CommandError, action, option
from domainkit.money import Money, MoneyError

NONE: Final = "none"
"""What clears a bound: ``--max-count none``."""


class Command(ActionCommand):
    """``list`` limits, a subject's ``usage``, ``set`` a limit, ``purge`` usage.

    ``set`` changes limits through ``LimitsUnitOfWork``, so each change is
    audited, as the API's are, under the operator's name.
    """

    help = "List limits, show a subject's usage, set a limit, or purge old usage."
    default_action = "list"

    @action(
        name="list",
        help="every limit, stored or default (the default)",
        options=[option("--kind")],
    )
    async def listed(self, *, kind: str | None) -> None:
        """One line per limit."""
        with _refusals():
            await self._list(kind)

    @action(
        help="what a subject used of its limits",
        options=[option("kind"), option("subject"), option("--currency")],
    )
    async def usage(self, *, kind: str, subject: str, currency: str | None) -> None:
        """One line per limit the subject has used."""
        with _refusals():
            await self._usage(kind, subject, currency)

    @action(
        name="set",
        help="make a limit or change its bounds",
        options=[
            option("code"),
            option("--kind", required=True),
            option("--currency", required=True),
            option("--subject", default="", help="one subject; none: every one"),
            option(
                "--direction",
                choices=[each.value for each in Direction],
                default="out",
            ),
            option("--per-transaction", help=f"an amount, or {NONE}"),
            option("--window", choices=[*(each.value for each in Window), NONE]),
            option("--max-amount", help=f"an amount, or {NONE}"),
            option("--max-count", help=f"a count, or {NONE}"),
            option("--time-zone"),
            option("--note"),
            option("--enable", action="store_true", help="turn the limit on"),
            option("--disable", action="store_true", help="turn the limit off"),
        ],
        operator=True,
    )
    async def configure(self, **options: Any) -> None:  # noqa: ANN401 - the options.
        """Make or change the limit.

        Raises:
            CommandError: Both ``--enable`` and ``--disable``.
        """
        if options["enable"] and options["disable"]:
            raise CommandError("--enable and --disable cannot go together.")
        with _refusals():
            await self._set(**options)

    @action(help="delete usage older than LIMITS.KEEP_USAGE", operator=True)
    async def purge(self) -> None:
        """Delete old usage, and say how much."""
        async with self.recorded("limits.purge") as recorded:
            recorded["usage"] = deleted = await purge_usage()
        self.write(f"Deleted the usage of {deleted} movement(s).")

    async def _list(self, kind: str | None) -> None:
        """One line per limit: the stored ones, then the defaults they leave."""
        statement = select(limits).order_by(
            limits.c.code,
            limits.c.kind,
            limits.c.direction,
            limits.c.currency,
            limits.c.subject,
        )
        if kind is not None:
            statement = statement.where(limits.c.kind == kind)
        async with connections.engine(limits_alias()).connect() as connection:
            stored = (await connection.execute(statement)).all()
        keys = {(r.code, r.kind, r.direction, r.currency, r.subject) for r in stored}
        rules = [
            rule
            for rule in map(default_rule, config().DEFAULTS)
            if (rule.code, rule.kind, rule.direction, rule.currency, rule.subject)
            not in keys
            and kind in (None, rule.kind)
        ]
        for row in stored:
            self.write(_describe(stored_rule(row), "stored"))
        for rule in rules:
            self.write(_describe(rule, "settings"))
        if not stored and not rules:
            self.write("No limits.")

    async def _usage(self, kind: str, subject: str, currency: str | None) -> None:
        """One line per limit that bounds the subject."""
        found = await usage_of(kind, subject, currency=currency)
        for each in found:
            self.write(_described_usage(each))
        if not found:
            self.write(f"No limit bounds {kind} {subject!r}.")

    async def _set(
        self,
        *,
        code: str,
        kind: str,
        currency: str,
        subject: str,
        direction: str,
        per_transaction: str | None,
        window: str | None,
        max_amount: str | None,
        max_count: str | None,
        time_zone: str | None,
        note: str | None,
        enable: bool,
        disable: bool,
        **options: object,
    ) -> None:
        """Make the limit, or change the bounds given; the rest stay."""
        async with LimitsUnitOfWork(readonly=True) as uow:
            found = await uow.limits.by_key(
                code, kind, Direction(direction), currency.upper(), subject
            )
        enabled = True if found is None else found.enabled
        if enable or disable:
            enabled = enable
        limit = await set_limit(
            SetLimit(
                code=code,
                kind=kind,
                currency=currency.upper(),
                subject=subject,
                direction=Direction(direction),
                per_transaction=_amount(
                    per_transaction,
                    currency,
                    None if found is None else found.per_transaction,
                ),
                window=_window(window, None if found is None else found.window),
                max_amount=_amount(
                    max_amount, currency, None if found is None else found.max_amount
                ),
                max_count=_count(max_count, None if found is None else found.max_count),
                time_zone=time_zone,
                enabled=enabled,
                note=note
                if note is not None
                else ("" if found is None else found.note),
            ),
            LimitsUnitOfWork(),
        )
        self.write(_describe(Rule.of(limit), "stored"))


def _amount(given: str | None, currency: str, kept: Money | None) -> Money | None:
    """An amount option: the one given, none, or the limit's own."""
    if given is None:
        return kept
    if given == NONE:
        return None
    return Money.of(given, currency.upper())


def _window(given: str | None, kept: Window | None) -> Window | None:
    """The window option: the one given, none, or the limit's own."""
    if given is None:
        return kept
    return None if given == NONE else Window(given)


def _count(given: str | None, kept: int | None) -> int | None:
    """The count option: the one given, none, or the limit's own."""
    if given is None:
        return kept
    if given == NONE:
        return None
    if not given.isdigit():
        raise CommandError(f"--max-count is a count, or {NONE}.")
    return int(given)


def _text(amount: object, currency: str) -> str:
    """An amount at its currency's places, with the code."""
    return str(Money.of(amount, currency))  # type: ignore[arg-type]


def _describe(rule: Rule, origin: str) -> str:
    """One line for a limit."""
    bounds = []
    if rule.per_transaction is not None:
        bounds.append(f"at most {_text(rule.per_transaction, rule.currency)} each")
    if rule.max_amount is not None:
        bounds.append(f"{_text(rule.max_amount, rule.currency)} a {rule.window}")
    if rule.max_count is not None:
        bounds.append(f"{rule.max_count} a {rule.window}")
    who = f"{rule.kind} {rule.subject!r}" if rule.subject else f"every {rule.kind}"
    off = "" if rule.enabled else " (disabled)"
    return (
        f"{rule.code}: {who}, {rule.direction} {rule.currency}: "
        f"{', '.join(bounds)} [{origin}]{off}"
    )


def _described_usage(usage: Usage) -> str:
    """One line for what a subject used of a limit."""
    if usage.window is None:
        return f"{usage.code}: at most {usage.per_transaction} each ({usage.direction})"
    parts = [f"{usage.used}"]
    if usage.max_amount is not None:
        parts.append(f"of {usage.max_amount}")
    parts.append(f"in {usage.count} movement(s)")
    if usage.max_count is not None:
        parts.append(f"of {usage.max_count}")
    since = usage.since.isoformat() if usage.since is not None else ""
    return f"{usage.code}: {' '.join(parts)} since {since} ({usage.direction})"


@contextmanager
def _refusals() -> Iterator[None]:
    """Report a limit the domain refuses as the command's error.

    Raises:
        CommandError: The domain, or an amount, refused.
    """
    try:
        yield
    except (DomainError, MoneyError) as exc:
        raise CommandError(str(exc)) from None
