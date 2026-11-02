"""``accounting``: reports, checks, holds, and reconciliation of the ledger."""

import asyncio
import uuid
from contextlib import nullcontext
from pathlib import Path

from sqlalchemy import select

from domainkit.contrib.accounting.holds import expire_holds
from domainkit.contrib.accounting.models import AccountId, PeriodId, amount_text
from domainkit.contrib.accounting.reconciliation import (
    CsvStatement,
    breaks_of,
    import_statement,
    reconcile,
)
from domainkit.contrib.accounting.reports import (
    general_ledger,
    trial_balance,
    verify_ledger,
)
from domainkit.contrib.accounting.tables import accounts, periods
from domainkit.contrib.accounting.uow import accounting_alias
from domainkit.db import connections
from domainkit.management import ActionCommand, CommandError, action, option

REPORT_OPTIONS = (
    option("target", nargs="?", metavar="PERIOD", help="the period, by name or id"),
    option("--format", choices=("csv", "json"), default="csv"),
    option("--output", type=Path, help="write the report there instead of stdout"),
)
ACCOUNT = option(
    "target", nargs="?", metavar="ACCOUNT", help="the account, by code or id"
)


class Command(ActionCommand):
    """Report a period, check the books, expire holds, and reconcile statements."""

    help = (
        "Write a period's trial balance or general ledger, check the books, "
        "release expired holds, import a statement, reconcile an account, or "
        "list what is unmatched."
    )

    @action(help="write a period's trial balance", options=REPORT_OPTIONS)
    async def trialbalance(
        self, *, target: str | None, format: str, output: Path | None
    ) -> None:
        """Write the period's trial balance."""
        period = await _period(_named(target, "trialbalance"))
        await self._report("trialbalance", period, format, output)

    @action(help="write a period's general ledger", options=REPORT_OPTIONS)
    async def ledger(
        self, *, target: str | None, format: str, output: Path | None
    ) -> None:
        """Write the period's general ledger."""
        period = await _period(_named(target, "ledger"))
        await self._report("ledger", period, format, output)

    @action(help="check that every entry balances and every balance sums its postings")
    async def verify(self) -> None:
        """Check the books."""
        await self._verify()

    @action(help="release the holds whose time is up", operator=True)
    async def expireholds(self) -> None:
        """Release expired holds, and say how many."""
        async with self.recorded("accounting.expireholds") as recorded:
            recorded["holds"] = expired = await expire_holds()
        self.write(f"Released {expired} expired hold(s).")

    @action(
        name="import",
        help="import a statement's CSV file into an account",
        options=[
            ACCOUNT,
            option("file", nargs="?", type=Path, help="the statement's CSV file"),
        ],
        operator=True,
    )
    async def import_(self, *, target: str | None, file: Path | None) -> None:
        """Import the statement."""
        account = await _account(_named(target, "import"))
        await self._account_action("import", account, file)

    @action(
        help="reconcile an account's statement lines", options=[ACCOUNT], operator=True
    )
    async def reconcile(self, *, target: str | None) -> None:
        """Match the account's lines, and say what is left."""
        account = await _account(_named(target, "reconcile"))
        await self._account_action("reconcile", account, None)

    @action(help="list an account's unmatched lines", options=[ACCOUNT])
    async def unmatched(self, *, target: str | None) -> None:
        """One line per break."""
        account = await _account(_named(target, "unmatched"))
        await self._account_action("unmatched", account, None)

    async def _verify(self) -> None:
        """Check every entry balances and every balance sums its postings."""
        problems = await verify_ledger()
        for problem in problems:
            self.write(f"{problem.kind}  {problem.id}  {problem.detail}")
        if problems:
            raise CommandError(f"The books have {len(problems)} problem(s).")
        self.write("The books balance.")

    async def _report(
        self, action: str, period_id: PeriodId, format: str, output: Path | None
    ) -> None:
        """Write a report of the period."""
        with (
            output.open("w", encoding="utf-8", newline="")
            if output is not None
            else nullcontext(self.stdout)
        ) as sink:
            if action == "trialbalance":
                report = await trial_balance(period_id)
                sink.write(report.to_csv() if format == "csv" else report.to_json())
            else:
                async for piece in general_ledger(period_id, format):  # type: ignore[arg-type]
                    sink.write(piece)

    async def _account_action(
        self, action: str, account_id: AccountId, file: Path | None
    ) -> None:
        """Import into, reconcile, or list the breaks of an account."""
        if action == "import":
            if file is None:
                raise CommandError("import needs the statement's file.")
            text = await asyncio.to_thread(file.read_text, encoding="utf-8-sig")
            async with self.recorded(
                "accounting.import", account=str(account_id), file=file.name
            ) as recorded:
                done = await import_statement(
                    account_id, CsvStatement(text, name=file.name)
                )
                recorded["imported"], recorded["skipped"] = done.imported, done.skipped
            self.write(f"Imported {done.imported} line(s); {done.skipped} known.")
        elif action == "reconcile":
            async with self.recorded(
                "accounting.reconcile", account=str(account_id)
            ) as recorded:
                result = await reconcile(account_id)
                recorded["matched"] = sum(result.matched.values())
                recorded["breaks"] = len(result.breaks)
            for rule, count in sorted(result.matched.items()):
                self.write(f"Matched {count} by {rule}.")
            self.write(f"{len(result.breaks)} unmatched.")
        else:
            for each in await breaks_of(account_id):
                self.write(
                    f"{each.booked_on}  {each.kind:17}  "
                    f"{amount_text(each.amount, each.currency)} {each.currency}"
                    f"  {each.reference}"
                )


def _named(target: str | None, name: str) -> str:
    """``target``, the period or account ``name`` acts on.

    Raises:
        CommandError: None was given.
    """
    if target is None:
        raise CommandError(f"{name} needs a period or an account.")
    return target


def _uuid(text: str) -> uuid.UUID | None:
    """``text`` as an id, if it is one."""
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


async def _period(target: str) -> PeriodId:
    """The id of the period named ``target``, or with that id."""
    column = periods.c.id if _uuid(target) is not None else periods.c.name
    value = _uuid(target) or target
    async with connections.engine(accounting_alias()).connect() as connection:
        found = await connection.scalar(select(periods.c.id).where(column == value))
    if found is None:
        raise CommandError(f"No period is {target!r}.")
    return PeriodId(found)


async def _account(target: str) -> AccountId:
    """The id of the account whose code is ``target``, or with that id."""
    column = accounts.c.id if _uuid(target) is not None else accounts.c.code
    value = _uuid(target) or target
    async with connections.engine(accounting_alias()).connect() as connection:
        found = await connection.scalar(select(accounts.c.id).where(column == value))
    if found is None:
        raise CommandError(f"No account is {target!r}.")
    return AccountId(found)
