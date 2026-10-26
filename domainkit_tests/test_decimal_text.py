"""One text for an amount: fixed point wherever domainkit writes a Decimal (FIN-82).

pydantic writes ``Decimal("1E+3")`` as ``"1E+3"``, which evidence wrote as
``"1000"`` and spreadsheets read as a float. Every place that writes an
amount as text now writes ``decimal_text``, and JSON strings stay strings.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

import pytest
from pydantic import BaseModel, PlainSerializer
from sqlalchemy import Column, Integer, MetaData, Numeric, Table

from domainkit.contrib.audit import Outcome
from domainkit.contrib.audit.personal import pseudonym
from domainkit.contrib.audit.recorder import audit_row, event_rows
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.contrib.evidence import Evidence
from domainkit.contrib.flags.store import DynamicSettingDefinition
from domainkit.contrib.messaging.registry import (
    dump_arguments,
    dump_event,
    load_event,
)
from domainkit.contrib.webhooks.delivery import body_of
from domainkit.db import fixtures
from domainkit.ddd import AggregateRoot, Digits, DomainEvent
from domainkit.http import pagination
from domainkit.resources.renderers import cell
from domainkit.resources.schemas import schema_for
from domainkit.utils.decimals import (
    MAX_FIXED_DIGITS,
    decimal_text,
    jsonable,
    with_decimal_text,
)

THOUSAND = Decimal("1E+3")
TINY_ZERO = Decimal("0E-8")


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("1E+3", "1000"),
        ("1.5E+2", "150"),
        ("-2.50E+1", "-25.0"),
        ("0E-8", "0.00000000"),
        ("1E-7", "0.0000001"),
        ("-1.25E-3", "-0.00125"),
        ("5.00", "5.00"),
        ("-12.50", "-12.50"),
        ("1234567890123456.78", "1234567890123456.78"),
        ("-0", "0"),
        ("-0.00", "0.00"),
        ("-0E-8", "0.00000000"),
        ("-0E+3", "0"),
        (f"1E-{MAX_FIXED_DIGITS}", "0." + "0" * (MAX_FIXED_DIGITS - 1) + "1"),
        (f"1E+{MAX_FIXED_DIGITS - 1}", "1" + "0" * (MAX_FIXED_DIGITS - 1)),
        (f"1E-{MAX_FIXED_DIGITS + 1}", f"1E-{MAX_FIXED_DIGITS + 1}"),
        (f"1E+{MAX_FIXED_DIGITS}", f"1E+{MAX_FIXED_DIGITS}"),
        ("1E+999999999", "1E+999999999"),
        ("NaN", "NaN"),
        ("-Infinity", "-Infinity"),
    ],
)
def test_amounts_are_written_in_fixed_point(value: str, text: str) -> None:
    """No exponent, the value's own places, and zero without a sign.

    Past ``MAX_FIXED_DIGITS`` on either side of the point, a value is no
    amount, and stays short: ``str``.
    """
    assert decimal_text(Decimal(value)) == text
    if Decimal(value).is_finite():
        assert Decimal(text) == Decimal(value)


@dataclass(frozen=True)
class Fee:
    """A nested dataclass."""

    amount: Decimal


class Quote(BaseModel):
    """A nested model."""

    price: Decimal
    shown: Annotated[Decimal, PlainSerializer(lambda value: "custom", return_type=str)]
    number: Annotated[Decimal, PlainSerializer(float, return_type=float)]


def test_pydantic_values_get_fixed_point_text_at_every_depth() -> None:
    """Fields, items, and members, by key or position; others are left alone.

    A serializer's own text and numbers stay as written, and so does a
    string that only looks like an amount.
    """
    value = {
        "fee": Fee(THOUSAND),
        "quote": Quote(price=TINY_ZERO, shown=THOUSAND, number=Decimal("2.5")),
        "list": [THOUSAND, "1E+3", 3],
        "tuple": (Decimal("-0.00"),),
        "set": {Decimal("2E+1")},
        "frozen": frozenset({Decimal("3E+2")}),
        7: Decimal("7E+1"),
        "none": None,
    }

    assert jsonable(value) == {
        "fee": {"amount": "1000"},
        "quote": {"price": "0.00000000", "shown": "custom", "number": 2.5},
        "list": ["1000", "1E+3", 3],
        "tuple": ["0.00"],
        "set": ["20"],
        "frozen": ["300"],
        "7": "70",
        "none": None,
    }


def test_values_without_decimals_come_back_as_they_were() -> None:
    """Nothing is copied when nothing changes, and odd pairs are left alone."""
    dumped = {"a": [1, "x"], "b": {"c": 2}}
    assert with_decimal_text({"a": [1, "x"], "b": {"c": 2}}, dumped) is dumped
    assert with_decimal_text([THOUSAND], ["1E+3", "extra"]) == ["1E+3", "extra"]
    assert with_decimal_text(object(), {"k": "1E+3"}) == {"k": "1E+3"}
    assert with_decimal_text(THOUSAND, 1000) == 1000
    assert with_decimal_text(Fee, {"amount": "1E+3"}) == {"amount": "1E+3"}
    assert with_decimal_text({"left_out": THOUSAND}, {}) == {}
    assert with_decimal_text(object(), ["1E+3"]) == ["1E+3"]


class Paid(DomainEvent):
    """An event with amounts."""

    order_id: UUID
    amount: Decimal
    fees: list[Fee] = field(default_factory=list)


def test_outbox_payloads_write_amounts_in_fixed_point() -> None:
    """Events and task arguments; an event reads back equal."""
    event = Paid(order_id=UUID(int=1), amount=THOUSAND, fees=[Fee(Decimal("-0"))])

    payload = dump_event(event)

    assert (payload["amount"], payload["fees"]) == ("1000", [{"amount": "0"}])
    assert load_event(Paid, payload) == event
    assert dump_arguments({"amount": TINY_ZERO, "n": 1}) == {
        "amount": "0.00000000",
        "n": 1,
    }


def test_audit_details_write_amounts_in_fixed_point() -> None:
    """Details given to ``record``, and the fields of audited events."""
    row = audit_row("pay", Outcome.SUCCESS, details={"amount": THOUSAND})
    event = Paid(order_id=UUID(int=1), amount=TINY_ZERO)
    [drafted] = event_rows([event], AuditSettings())

    assert row.row["details"] == {"amount": "1000"}
    assert drafted.row["details"]["amount"] == "0.00000000"


def test_pseudonyms_of_equal_amounts_are_equal() -> None:
    """A personal amount hashes its fixed-point text, as its audit details hold it."""
    key = b"k" * 32

    assert pseudonym(key, "salary", THOUSAND) == pseudonym(key, "salary", "1000")
    assert pseudonym(key, "salary", Decimal("1000")) == pseudonym(
        key, "salary", THOUSAND
    )
    assert pseudonym(key, "salary", "1E+3") != pseudonym(key, "salary", THOUSAND)


def test_evidence_zero_has_no_sign_and_huge_values_stay_short() -> None:
    """Evidence wrote fixed point already; now ``-0.00`` too, and boundedly."""
    evidence = Evidence(
        kind="payment.settled",
        subject="payment:1",
        facts={"amount": Decimal("-0.00"), "odd": Decimal("1E+999999999")},
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    facts = json.loads(evidence.canonical())["facts"]

    assert facts == {"amount": "0.00", "odd": "1E+999999999"}


class Invoice(AggregateRoot[UUID]):
    """An aggregate with amounts, with and without digits."""

    total: Annotated[Decimal, Digits(12, 2)]
    rate: Decimal


def test_api_responses_and_exports_write_amounts_in_fixed_point() -> None:
    """Read schemas write text in JSON, keep Decimals in Python, and CSV agrees."""
    schema = schema_for(Invoice)
    shown = schema(id=UUID(int=1), version=1, total=Decimal("1E+3"), rate=TINY_ZERO)

    assert shown.model_dump(mode="json")["total"] == "1000"
    assert shown.model_dump(mode="json")["rate"] == "0.00000000"
    assert json.loads(shown.model_dump_json())["total"] == "1000"
    assert shown.model_dump()["total"] == THOUSAND
    assert schema.model_json_schema(mode="serialization")["properties"]["total"][
        "type"
    ] == ("string")
    assert cell(THOUSAND) == "1000"
    assert cell("1E+3") == "1E+3"


def test_cursors_fixtures_flags_and_webhooks_write_amounts_in_fixed_point() -> None:
    """The other places an amount leaves as text."""
    table = Table("amounts", MetaData(), Column("id", Integer), Column("x", Numeric))
    setting = DynamicSettingDefinition("fees.rate", Decimal, THOUSAND, "A rate.")
    body = body_of("paid", {"amount": THOUSAND}, datetime(2026, 1, 1, tzinfo=UTC))

    assert pagination._to_json(Decimal("-0.00")) == "0.00"
    assert pagination._to_json(THOUSAND) == "1000"
    assert fixtures._encoded(table.c.x, TINY_ZERO) == "0.00000000"
    assert setting.default_json() == "1000"
    assert setting.checked(Decimal("3E+1")) == "30"
    assert json.loads(body)["data"] == {"amount": "1000"}
