"""MinLength, Pattern, and Range: what request bodies may hold, and OpenAPI says."""

from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import pydantic
import pytest

from domainkit.ddd import (
    AggregateRoot,
    MaxLength,
    MinLength,
    Pattern,
    Range,
    ValueObject,
)
from domainkit.resources import schema_for


class Sku(ValueObject):
    """A single-field value object: its markers are the field's."""

    value: Annotated[str, MaxLength(8), Pattern(r"[A-Z]{3}-[0-9]{4}")]


class Item(AggregateRoot[UUID]):
    """Every marker on one aggregate."""

    name: Annotated[str, MinLength(3), MaxLength(20)]
    sku: Sku
    quantity: Annotated[int, Range(min=1, max=100)]
    discount: Annotated[Decimal, Range(min=0, max=Decimal("0.5"))]
    score: Annotated[float, Range(max=5.0)]


ItemIn = schema_for(
    Item, write=True, include=["name", "sku", "quantity", "discount", "score"]
)
GOOD: dict[str, Any] = {
    "name": "Lamp",
    "sku": "LMP-0042",
    "quantity": 5,
    "discount": "0.10",
    "score": 4.5,
}


def test_openapi_shows_each_limit() -> None:
    """Lengths, the anchored pattern, and the bounds are in the schema."""
    properties = ItemIn.model_json_schema()["properties"]

    assert (properties["name"]["minLength"], properties["name"]["maxLength"]) == (3, 20)
    assert properties["sku"]["pattern"] == "^(?:[A-Z]{3}-[0-9]{4})$"
    assert (properties["quantity"]["minimum"], properties["quantity"]["maximum"]) == (
        1,
        100,
    )
    assert properties["score"]["maximum"] == 5.0


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"name": "La"}, "string_too_short"),
        ({"sku": "lamp"}, "string_pattern_mismatch"),
        ({"sku": "LMP-0042x"}, "string_too_long"),
        ({"quantity": 0}, "greater_than_equal"),
        ({"quantity": 101}, "less_than_equal"),
        ({"discount": "0.6"}, "less_than_equal"),
        ({"score": 5.1}, "less_than_equal"),
    ],
)
def test_bodies_outside_the_limits_are_refused(
    change: dict[str, Any], error: str
) -> None:
    """Each marker refuses what it rules out, before any use case runs."""
    assert ItemIn(**GOOD).quantity == 5
    with pytest.raises(pydantic.ValidationError) as caught:
        ItemIn(**{**GOOD, **change})
    assert caught.value.errors()[0]["type"] == error


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: MinLength(0), "at least 1"),
        (lambda: Pattern("("), "is not a regex"),
        (lambda: Range(), "min, max, or both"),
        (lambda: Range(min=5, max=1), "above max"),
    ],
)
def test_markers_refuse_what_cannot_hold(make: Any, message: str) -> None:  # noqa: ANN401
    """A marker that could never pass is refused where it is declared."""
    with pytest.raises(ValueError, match=message):
        make()
