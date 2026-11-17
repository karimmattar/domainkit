"""What an act shows on a phone: whole, or refused; never cut unsaid (user report A22).

A screen holds a title and six labelled lines of 64 characters
(``Shown``, ADR 0144). ``laid_out`` puts an act's values on those lines: a
text on one, a list over as many as it takes ("Adds", "Adds (2)"). With
``whole`` (the default), what would be cut or left out is refused
(``TooMuchToShow``, 422); otherwise it is cut and counted (ADR 0173).
"""

from typing import Final

import pytest

from domainkit.contrib.auth.actions import Shown
from domainkit.contrib.auth.screens import Details, TooMuchToShow, laid_out
from domainkit.http.errors import status_code_for
from domainkit.i18n import override

GROUPS: Final = tuple(f"group_{each:02}" for each in range(9))
"""Nine items of eight characters: six fit on a line, with their commas."""
MANY: Final = tuple(f"group_{each:02}" for each in range(40))
LONG: Final = "x" * 65
"""One character more than a line holds."""


def test_a_list_goes_over_as_many_lines_as_it_takes() -> None:
    """Each line holds the items that fit; the next lines are numbered."""
    lines = laid_out({"User": "ann", "Adds": GROUPS})

    assert lines == [
        ("User", "ann"),
        ("Adds", "group_00, group_01, group_02, group_03, group_04, group_05"),
        ("Adds (2)", "group_06, group_07, group_08"),
    ]
    assert Shown("Change a user's groups", lines).details == tuple(lines)
    assert laid_out({"Adds": ()}) == [("Adds", "")]


def test_an_item_that_could_be_read_as_two_is_quoted() -> None:
    """A comma, a quote, spaces around it: quoted; a new line escaped."""
    lines = laid_out({"Adds": ("readers, admins", "clerks", 'say "hi"', "a\nb", " x")})

    assert lines == [
        ("Adds", '"readers, admins", clerks, "say \\"hi\\"", a\\nb, " x"'),
    ]


def test_whole_refuses_what_it_would_cut_or_leave_out() -> None:
    """A text, an item, or a label too long; a label twice; too many lines."""
    refused: list[Details] = [
        {"Reason": LONG},
        {"Adds": ("ok", LONG)},
        {LONG: "v"},
        {"": "v"},
        [("User", "a"), ("User", "b")],
        {f"k{each}": "v" for each in range(7)},
        {"User": "ann", "Adds": MANY},
        {"Adds": ("a" * 40, "b" * 40), "Adds (2)": "x"},
    ]

    for details in refused:
        with pytest.raises(TooMuchToShow):
            laid_out(details)
    assert len(laid_out({f"k{each}": "x" * 64 for each in range(6)})) == 6
    assert TooMuchToShow().code == "too_much_to_show"
    assert status_code_for(TooMuchToShow()) == 422


def test_counted_cuts_each_value_and_says_how_many_more() -> None:
    """Off whole: a text cut ends with …; a list's last line counts the rest.

    Every value gets a line; the lines left go to the lists, in order.
    """
    lines = laid_out(
        {"User": "ann", "Reason": "x" * 100, "Adds": MANY, "Takes away": ("a",)},
        whole=False,
    )

    assert lines == [
        ("User", "ann"),
        ("Reason", "x" * 63 + "…"),
        ("Adds", "group_00, group_01, group_02, group_03, group_04, group_05"),
        ("Adds (2)", "group_06, group_07, group_08, group_09, group_10, group_11"),
        ("Adds (3)", "group_12, group_13, group_14, group_15, group_16 and 23 more"),
        ("Takes away", "a"),
    ]
    assert laid_out({"Adds": ("g" * 100, "b", "c")}, whole=False) == [
        ("Adds", "g" * 63 + "…"),
        ("Adds (2)", "b, c"),
    ]
    last = laid_out(
        {**{f"k{each}": "v" for each in range(5)}, "Adds": ("g" * 100, "b")},
        whole=False,
    )[-1]
    assert last == ("Adds", "g" * 52 + "… and 1 more")


def test_counted_values_past_the_screen_are_counted_never_dropped_unsaid() -> None:
    """Five values and how many more; a label twice is counted too."""
    many = laid_out({f"k{each}": "v" for each in range(9)}, whole=False)
    twice = laid_out([("User", "ann"), ("User", "again"), ("b", "2")], whole=False)
    worded = laid_out(
        {f"k{each}": "v" for each in range(7)},
        whole=False,
        more=lambda count: f"{count} more arguments",
    )

    assert many == [*((f"k{each}", "v") for each in range(5)), ("…", "4 more values")]
    assert twice == [("User", "ann"), ("b", "2"), ("…", "1 more value")]
    assert worded[-1] == ("…", "2 more arguments")


def test_counted_numbered_labels_never_repeat() -> None:
    """A label taken, or one cut to the same words, ends the list's lines there."""
    long = laid_out({"L" * 63: MANY}, whole=False)
    taken = laid_out({"Adds": MANY, "Adds (2)": "x"}, whole=False)

    for lines in (long, taken):
        labels = [label for label, _ in lines]
        assert len(set(labels)) == len(labels)
        Shown("t", lines)
    assert [label for label, _ in long] == ["L" * 63, "L" * 63 + "…"]
    assert long[-1][1].endswith(" and 29 more")
    assert taken[0][1].endswith(" and 35 more")
    assert taken[1] == ("Adds (2)", "x")


def test_numbered_labels_and_counts_speak_the_request_language() -> None:
    """The words a screen adds are in the request's language: Arabic here."""
    with override("ar"):
        lines = laid_out({"يضيف": MANY[:10]}, whole=False)
        counted = laid_out({"يضيف": MANY}, whole=False)

    assert [label for label, _ in lines] == ["يضيف", "يضيف (2)"]
    assert "أخرى" in counted[-1][1]
