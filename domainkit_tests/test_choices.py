"""``TextChoices`` and ``IntegerChoices``: enums whose members carry labels."""

from pydantic import TypeAdapter

from domainkit.ddd import IntegerChoices, TextChoices
from domainkit.i18n.translation import gettext_lazy


class Status(TextChoices):
    """Text values; one labelled lazily, one from its name."""

    DRAFT_COPY = "draft"
    PAID = "paid", gettext_lazy("Paid in full")


class Priority(IntegerChoices):
    """Whole numbers; one labelled, one not."""

    LOW = 1
    HIGH = 3, "Urgent"


def test_members_are_their_values_with_labels() -> None:
    """Stored and compared by value; shown by label, or the name in words."""
    assert Status("paid") is Status.PAID
    assert Status.PAID.value == "paid"
    assert isinstance(Status.PAID, str)
    assert str(Status.DRAFT_COPY) == "draft"
    assert (Status.PAID.label, Status.DRAFT_COPY.label) == (
        "Paid in full",
        "Draft Copy",
    )
    assert Priority(3) is Priority.HIGH
    assert Priority.HIGH + 1 == 4
    assert (Priority.HIGH.label, Priority.LOW.label) == ("Urgent", "Low")


def test_choices_and_labels_list_every_member_in_order() -> None:
    """Django's ``choices``, and the labels by value."""
    assert Status.choices() == [("draft", "Draft Copy"), ("paid", "Paid in full")]
    assert Priority.labels() == {1: "Low", 3: "Urgent"}


def test_a_request_and_its_schema_see_only_the_values() -> None:
    """Validation and OpenAPI take the values, as for any enum."""
    adapter = TypeAdapter(Status)

    assert adapter.validate_python("paid") is Status.PAID
    assert adapter.json_schema()["enum"] == ["draft", "paid"]
    assert TypeAdapter(Priority).dump_python(Priority.HIGH, mode="json") == 3
