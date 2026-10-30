"""Reconciliation: statements read, imported once, and matched to postings."""

import uuid
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

import pytest

from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.accounting import (
    AccountId,
    AccountingUnitOfWork,
    AccountNotFound,
    InvalidStatement,
    Line,
    PostEntry,
    post_entry,
)
from domainkit.contrib.accounting.reconciliation import (
    Break,
    ByAmountAndDate,
    ByReference,
    CsvStatement,
    MatchingRule,
    PostingItem,
    StatementItem,
    StatementLine,
    breaks_of,
    configured_rules,
    import_statement,
    reconcile,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit.money import Money
from domainkit_tests.test_accounting import (
    TODAY,
    account,
    books,  # noqa: F401 - the fixture.
    period,
    refused,
    usd,
)

pytestmark = pytest.mark.anyio
HEADER: Final = "id,date,amount,currency,reference,description\n"
WINDOW: Final = timedelta(days=3)


def lines(text: str) -> list[StatementLine]:
    """The lines of a statement with the default columns."""
    return list(CsvStatement(HEADER + text).lines())


def test_a_csv_statement_reads_exact_amounts() -> None:
    """Amounts are text to the last place; references and descriptions are trimmed."""
    (first, second) = lines(
        "b-1,2026-10-15,10.50,USD,s-1, card sale \nb-2,2026-10-16,-0.0001,CLF,,\n"
    )
    assert first == StatementLine(
        "b-1", date(2026, 10, 15), usd("10.50"), "s-1", "card sale"
    )
    assert second.amount == Money.of("-0.0001", "CLF")
    assert (second.reference, second.description) == ("", "")


def test_a_statement_maps_a_banks_own_columns() -> None:
    """``columns`` renames them, and ``delimiter`` splits them."""
    statement = CsvStatement(
        "Ref;When;Value;Ccy\nx;2026-10-01;+3;USD\n",
        name="bank.csv",
        columns={"id": "Ref", "date": "When", "amount": "Value", "currency": "Ccy"},
        delimiter=";",
    )
    (line,) = statement.lines()
    assert (statement.name, line.external_id, line.amount) == (
        "bank.csv",
        "x",
        usd("3"),
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("id,date,amount\n", "needs the columns"),
        (HEADER + ",2026-10-15,1,USD,,\n", "id has"),
        (HEADER + "b-1,2026-10-15,1e3,USD,,\n", "not an amount"),
        (HEADER + "b-1,2026-10-15,1.5.0,USD,,\n", "not an amount"),
        (HEADER + "b-1,2026-10-15,1.00001,USD,,\n", "not an amount"),
        (HEADER + "b-1,2026-10-15,1 000,USD,,\n", "not an amount"),
        (HEADER + "b-1,15/10/2026,1,USD,,\n", "not a date"),
        (HEADER + "b-1,2026-10-15,1.005,USD,,\n", "does not fit"),
        (HEADER + "b-1,2026-10-15,1,XXX,,\n", "does not fit"),
        (HEADER + f"{'i' * 129},2026-10-15,1,USD,,\n", "id has"),
    ],
)
def test_malformed_statements_are_refused(text: str, message: str) -> None:
    """Missing columns, and rows whose id, date, amount, or currency is not one."""
    with refused(InvalidStatement, match=message):
        list(CsvStatement(text).lines())


def test_long_references_and_descriptions_are_cut() -> None:
    """To the columns' lengths."""
    (line,) = lines(f"b,2026-10-15,1,USD,{'r' * 200},{'d' * 300}\n")
    assert (len(line.reference), len(line.description)) == (128, 255)


def item(amount: str, day: int = 15, reference: str = "") -> StatementItem:
    """A statement line, as rules see it."""
    return StatementItem(
        uuid.uuid4(), date(2026, 10, day), Decimal(amount), "USD", reference, ""
    )


def posting(amount: str, day: int = 15, reference: str = "") -> PostingItem:
    """A posting, as rules see it."""
    return PostingItem(
        uuid.uuid4(),
        uuid.uuid4(),
        date(2026, 10, day),
        Decimal(amount),
        "USD",
        reference,
        "",
    )


def test_by_reference_pairs_the_nearest_posting_of_a_reference() -> None:
    """Same reference, amount, and currency, within the window; nearest first."""
    line, far, near = item("5", 15, "r"), posting("5", 17, "r"), posting("5", 14, "r")
    other = item("5", 15, "")
    euro = PostingItem(
        uuid.uuid4(), uuid.uuid4(), date(2026, 10, 15), Decimal(5), "EUR", "r", ""
    )
    late = posting("5", 19, "r")
    wrong = posting("6", 15, "r")

    pairs = list(
        ByReference().match([line, other], [far, near, euro, late, wrong], WINDOW)
    )

    assert pairs == [(line, near)]
    assert list(ByReference().match([item("5", 15, "r")], [late], WINDOW)) == []
    taken = list(
        ByReference().match([item("5", 15, "r"), item("5", 15, "r")], [near], WINDOW)
    )
    assert len(taken) == 1


def test_by_amount_and_date_pairs_only_lone_candidates() -> None:
    """Two candidates on either side leave both for a person."""
    alone, lone = item("7", 15), posting("7", 16)
    twins = [item("3", 15), item("3", 15)]
    one = posting("3", 15)
    split = item("9", 15)
    choices = [posting("9", 14), posting("9", 16)]

    pairs = list(
        ByAmountAndDate().match([alone, *twins, split], [lone, one, *choices], WINDOW)
    )

    assert pairs == [(alone, lone)]
    assert (
        list(ByAmountAndDate().match([item("7", 15)], [posting("7", 19)], WINDOW)) == []
    )
    assert list(ByAmountAndDate().match([item("7", 15)], [posting("7", 18)], WINDOW))


class Twice:
    """A rule that pairs one line with two postings: a bug in a project's rule."""

    name = "twice"

    def match(
        self,
        lines: Sequence[StatementItem],
        postings: Sequence[PostingItem],
        window: timedelta,
    ) -> Iterator[tuple[StatementItem, PostingItem]]:
        """Pair the first line with every posting."""
        for each in postings:
            yield lines[0], each


class Nameless:
    """Not a rule: it has no ``name``."""

    def match(
        self, lines: object, postings: object, window: object
    ) -> Iterable[object]:
        """Nothing."""
        return ()


class Needy:
    """Not a rule the setting can make: it needs an argument."""

    def __init__(self, threshold: int) -> None:
        """Keep ``threshold``."""
        self.threshold = threshold


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("domainkit_tests.test_accounting_reconciliation.Nameless", "no name or match"),
        ("domainkit_tests.test_accounting_reconciliation.Needy", "taking no arguments"),
        (
            "domainkit_tests.test_accounting_reconciliation.Missing",
            "taking no arguments",
        ),
    ],
)
@pytest.mark.usefixtures("books")
async def test_rules_that_are_not_rules_fail_setup(path: str, message: str) -> None:
    """The setting's paths are made into rules when the app is ready."""
    with (
        override_settings(ACCOUNTING={"MATCHING_RULES": [path]}),
        pytest.raises(ImproperlyConfigured, match=message),
    ):
        configured_rules()
    assert isinstance(configured_rules()[0], MatchingRule)
    assert registry.is_installed("domainkit.contrib.accounting")


async def sales(*amounts: tuple[str, str, date]) -> AccountId:
    """A bank account with a sale per ``(reference, amount, day)``; its id."""
    await period()
    bank, income = await account("1000"), await account("4000")
    for reference, amount, day in amounts:
        await post_entry(
            PostEntry(
                source="shop",
                reference=reference,
                booked_on=day,
                lines=(
                    Line.debit(bank.id, usd(amount)),
                    Line.credit(income.id, usd(amount)),
                ),
            ),
            AccountingUnitOfWork(),
        )
    return bank.id


@pytest.mark.usefixtures("books")
async def test_a_statement_is_imported_once() -> None:
    """Lines are stored by their id on the statement; known ones are skipped."""
    bank = await sales()

    first = await import_statement(
        bank, CsvStatement(HEADER + "b-1,2026-10-15,1,USD,,\n")
    )
    second = await import_statement(
        bank, CsvStatement(HEADER + "b-1,2026-10-15,1,USD,,\nb-2,2026-10-16,2,USD,,\n")
    )
    empty = await import_statement(bank, CsvStatement(HEADER))

    assert (first.imported, first.skipped) == (1, 0)
    assert (second.imported, second.skipped) == (1, 1)
    assert (empty.imported, empty.skipped) == (0, 0)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("b-1,2026-10-15,1,EUR,,\n", "account's currency"),
        ("b-1,2026-10-15,1,USD,,\nb-1,2026-10-16,2,USD,,\n", "same id"),
        (
            "b-1,2026-10-15,1,USD,,\nb-2,2026-10-16,2,USD,,\nb-3,2026-10-16,2,USD,,\n",
            "at most",
        ),
    ],
)
@pytest.mark.usefixtures("books")
async def test_a_statement_that_does_not_fit_imports_nothing(
    text: str, message: str
) -> None:
    """Another currency, a repeated id, too many lines: nothing is stored."""
    bank = await sales()
    with (
        override_settings(ACCOUNTING={"MAX_STATEMENT_LINES": 2}),
        refused(InvalidStatement, match=message),
    ):
        await import_statement(bank, CsvStatement(HEADER + text))
    assert (await import_statement(bank, CsvStatement(HEADER))).imported == 0
    with override_settings(ACCOUNTING={"MAX_STATEMENT_LINES": 2}):
        done = await import_statement(
            bank,
            CsvStatement(HEADER + "b-1,2026-10-15,1,USD,,\nb-2,2026-10-16,2,USD,,\n"),
        )
    assert done.imported == 2


@pytest.mark.usefixtures("books")
async def test_imports_and_reconciliations_need_an_account() -> None:
    """An id no account has is not found."""
    nobody = AccountId(uuid.uuid4())
    with refused(AccountNotFound):
        await import_statement(nobody, CsvStatement(HEADER))
    with refused(AccountNotFound):
        await reconcile(nobody)
    with refused(AccountNotFound):
        await breaks_of(nobody)


@pytest.mark.usefixtures("books")
async def test_reconciliation_matches_and_records_breaks() -> None:
    """Rules match in order; what is left is a break, within the statements' dates."""
    bank = await sales(
        ("s-1", "10.50", date(2026, 10, 15)),
        ("s-2", "4.00", date(2026, 10, 16)),
        ("s-3", "8.00", date(2026, 10, 17)),
        ("s-4", "2.00", date(2026, 10, 1)),
    )
    await import_statement(
        bank,
        CsvStatement(
            HEADER
            + "b-1,2026-10-15,10.50,USD,s-1,\n"
            + "b-2,2026-10-18,4.00,USD,,transfer\n"
            + "b-3,2026-10-17,-3.00,USD,,fee\n"
        ),
    )

    done = await reconcile(bank)
    again = await reconcile(bank)
    found = await breaks_of(bank)

    assert done.matched == {"reference": 1, "amount_and_date": 1}
    assert [(b.kind, b.amount, b.reference) for b in done.breaks] == [
        ("unmatched_line", Decimal("-3.0000"), ""),
        ("unmatched_posting", Decimal("8.0000"), "s-3"),
    ]
    assert (again.matched, again.breaks) == ({}, done.breaks)
    assert found == done.breaks
    assert found[0] == Break(
        "unmatched_line",
        found[0].statement_line_id,
        None,
        date(2026, 10, 17),
        Decimal("-3.0000"),
        "USD",
        "",
    )


@pytest.mark.usefixtures("books")
async def test_an_account_without_statements_has_no_breaks() -> None:
    """Postings count as breaks only within the days the statements cover."""
    bank = await sales(("s-1", "1.00", TODAY))
    done = await reconcile(bank)
    assert (done.matched, done.breaks, await breaks_of(bank)) == ({}, [], [])


@pytest.mark.usefixtures("books")
async def test_a_rule_that_pairs_twice_is_refused() -> None:
    """A line in two pairs would match it twice: nothing is recorded."""
    bank = await sales(("s-1", "1.00", TODAY), ("s-2", "1.00", TODAY))
    await import_statement(bank, CsvStatement(HEADER + "b-1,2026-10-30,1,USD,,\n"))
    path = "domainkit_tests.test_accounting_reconciliation.Twice"
    with (
        override_settings(ACCOUNTING={"MATCHING_RULES": [path]}),
        pytest.raises(ImproperlyConfigured, match="paired a line or a posting twice"),
    ):
        await reconcile(bank)
    assert (await reconcile(bank)).matched == {}
