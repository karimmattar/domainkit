"""Email, URL, Slug, IPAddress: Django's field types, held everywhere."""

import pytest
from pydantic import ValidationError
from sqlalchemy import MetaData, String

from domainkit.db import table_for
from domainkit.ddd import (
    URL,
    AggregateRoot,
    Command,
    Email,
    InvalidField,
    IPAddress,
    IPAddressFormat,
    Slug,
)
from domainkit.resources import schema_for


class Site(AggregateRoot[int]):
    """An aggregate with one field of each type."""

    contact: Email
    slug: Slug
    home: URL | None = None
    last_ip: IPAddress | None = None


def site(**changes: object) -> Site:
    """A valid site, with ``changes``."""
    values: dict[str, object] = {"id": 1, "contact": "ops@example.com", "slug": "main"}
    return Site(**{**values, **changes})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "good", "bad"),
    [
        ("contact", "a.b+c@mail.example.org", "not-an-address"),
        ("contact", "x@y.co", "x@y"),
        ("slug", "release_2-0", "has space"),
        ("home", "https://example.com/a?b=1#c", "ftp://example.com"),
        ("home", "http://localhost:8000", "https://user@evil.example"),
        ("last_ip", "2001:db8::1", "300.1.1.1"),
        ("last_ip", "192.168.0.1", "localhost"),
    ],
)
def test_each_type_holds_on_the_object(field: str, good: str, bad: str) -> None:
    """Made or changed, a value its type rules out is refused, naming the field."""
    made = site(**{field: good})
    assert getattr(made, field) == good

    with pytest.raises(InvalidField) as caught:
        site(**{field: bad})
    assert caught.value.field == field
    with pytest.raises(InvalidField):
        setattr(made, field, bad)


def test_the_request_schema_checks_the_text_types() -> None:
    """Patterns and lengths reach the schema, and so OpenAPI."""
    schema = schema_for(Site, write=True, include=["contact", "slug", "home"])

    with pytest.raises(ValidationError):
        schema.model_validate({"contact": "nope", "slug": "ok"})
    properties = schema.model_json_schema()["properties"]
    assert properties["contact"]["maxLength"] == 254
    assert "pattern" in properties["slug"]


def test_an_address_is_checked_when_a_command_is_made() -> None:
    """A command checks its markers, the IP address's among them."""

    class Ban(Command):
        address: IPAddress

    assert Ban(address="10.0.0.1").address == "10.0.0.1"
    with pytest.raises(InvalidField):
        Ban(address="10.0.0.256")


def test_an_address_of_one_version() -> None:
    """``IPAddressFormat(version=4)`` takes IPv4 only; other versions are refused."""
    four = IPAddressFormat(version=4)

    assert four.problem("10.0.0.1") is None
    assert four.problem("::1") == "Enter a valid IPv4 address."
    assert four.problem(7) is None
    with pytest.raises(ValueError, match="version 4 or 6"):
        IPAddressFormat(version=5)


def test_columns_are_sized_by_the_types() -> None:
    """Each is text of its longest value."""
    table = table_for(Site, "framework_test_sites", metadata=MetaData())
    lengths = {
        name: column.type.length
        for name, column in table.columns.items()
        if isinstance(column.type, String)
    }

    assert lengths == {"contact": 254, "slug": 50, "home": 2048, "last_ip": 45}
