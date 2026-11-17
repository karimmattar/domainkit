"""What an act shows on a phone: whole, or refused; never cut unsaid (user report A22).

A screen holds a title and six labelled lines of 64 characters
(``Shown``, ADR 0144). ``laid_out`` puts an act's values on those lines: a
text on one, a list over as many as it takes ("Adds", "Adds (2)"). With
``whole`` (the default), what would be cut or left out is refused
(``TooMuchToShow``, 422); otherwise it is cut and counted (ADR 0173).

Text a person typed (``Typed``) is checked first: with
``AUTH.AUTHORIZATIONS.TYPED_LINKS`` refused, one that holds a link, an
email address, or a number to call is refused (``LinkNotShown``, 422),
read as a person reads it (user report A17, ADR 0180).
"""

from decimal import Decimal
from typing import Final
from uuid import UUID

import pytest

from domainkit.contrib.auth.actions import Shown
from domainkit.contrib.auth.screens import (
    Details,
    LinkNotShown,
    TooMuchToShow,
    Typed,
    check_typed,
    holds_link,
    laid_out,
    named,
    on_typed_text,
)
from domainkit.http.errors import status_code_for
from domainkit.i18n import override
from domainkit_tests.conftest import UseAuth

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


def test_two_values_never_show_alike() -> None:
    """A backslash is escaped too: a new line and a backslash and "n" differ.

    So do a zero-width space and the six characters that write it, in a
    text and in a list's items (review of ADR 0173).
    """
    pairs = [
        ("a\nb", "a\\nb"),
        ("admins​", "admins\\u200b"),
        ('say "hi"', 'say \\"hi\\"'),
    ]

    for one, other in pairs:
        texts = laid_out({"Name": one}), laid_out({"Name": other})
        items = laid_out({"Adds": (one,)}), laid_out({"Adds": (other,)})
        assert texts[0] != texts[1], (one, other)
        assert items[0] != items[1], (one, other)
    assert laid_out({"Name": "a\\b\nc"}) == [("Name", "a\\\\b\\nc")]
    assert laid_out({"Adds": ('a"b',)}) == [("Adds", '"a\\"b"')]


def test_json_values_and_items_show_as_compact_json() -> None:
    """A dict is one JSON value; a list's items each one; bytes are refused."""
    lines = laid_out(
        {
            "meta": {"k": "v", "a": 1},
            "n": 2,
            "on": True,
            "none": None,
            "lines": [{"a": 1}, ["b", "c"], 3, "d"],
        }
    )
    counted = laid_out(
        {"lines": [{"account": "a" * 30, "n": each} for each in range(9)]}, whole=False
    )

    assert lines == [
        ("meta", '{"a":1,"k":"v"}'),
        ("n", "2"),
        ("on", "true"),
        ("none", "null"),
        ("lines", '{"a":1}, ["b","c"], 3, d'),
    ]
    assert counted[0] == ("lines", '{"account":"' + "a" * 30 + '","n":0}')
    assert counted[-1] == (
        "lines (6)",
        '{"account":"' + "a" * 30 + '","n":5} and 3 more',
    )
    for value in (b"admins", {"a", "b"}, Decimal("1.5"), [b"x"]):
        with pytest.raises(TypeError):
            laid_out({"Bad": value})
    with pytest.raises(TooMuchToShow):
        laid_out({"lines": [{"account": "a" * 70}]})


def test_what_an_act_is_on_shows_its_id_whole_and_its_name_cut_to_fit() -> None:
    """Never too much to show: the act binds the id, and its end shows.

    The name has the 48 characters the id's last 12 leave, so most
    addresses show whole; no name shows as "(erased)" (review of ADR 0173).
    """
    user_id = UUID(int=42)
    short = named("ann", user_id)
    long = named("u" * 150, user_id)
    erased = named("", user_id)

    assert short == "ann (…00000000002a)"
    assert named("firstname.lastname@company-example.com", user_id) == (
        "firstname.lastname@company-example.com (…00000000002a)"
    )
    assert named("x", 7) == "x (7)"
    assert long == f"{'u' * 47}… (…00000000002a)"
    assert len(long) == 64
    assert erased == "(erased) (…00000000002a)"
    assert laid_out({"User": long}) == [("User", long)]
    with override("ar"):
        assert named("", user_id) == "(مُزال) (…00000000002a)"


LINKS: Final = (
    "visit evil.example.com",
    "visit evil.example",
    "x.onion",
    "wiki.internal",
    "evil.com/path",
    "Evil.COM.",
    "https://x",
    "hxxp://evil",
    "hxxps[:]//evil",
    "example[.]com",
    "example [dot] com",
    "x {.} com",
    "name(at)example(dot)com",
    "ann@example.com",
    "ann [at] example",
    "contact @support",
    "www.anything",
    "203.0.113.7",
    "mailto:ann",
    "tel:+15550100",
    "evil.zip",
    "Q3.report",
    "evil.c0m",
    "x.xn--p1ai",
    "\u0441\u0430\u0439\u0442.\u0440\u0444",
    "\u0e01.\u0e44\u0e17\u0e22",
    "evil.\u0441\u043em",
    "evil.c\u0335om",
    "\U0001d41e\U0001d42f\U0001d422\U0001d425.\U0001d41c\U0001d428\U0001d426",
    "\uff45\uff56\uff49\uff4c\uff0e\uff43\uff4f\uff4d",
    "evil\uff0ecom",
    "evil\u3002com",
    "evil\uff61com",
    "evil\ufe52com",
    "evil\u2024com",
    "evil\u00b7com",
    "evil\u30fbcom",
    "abc\u0660com",
    "evil\u200b.com",
    "e\u200bvil.c\u200dom",
    "evil\u2060.com",
    "evil\u00ad.com",
    "evil\ufeff.com",
)
"""Links, as written and as written to pass for something else."""
NUMBERS: Final = (
    "Call +1 555 010 0100 to confirm",
    "Call (555) 010-0100",
    "Call 555-010-0100",
    "Call 555.010.0100",
    "Call 555/010/0100",
    "Call 555\u2013010\u20130100",
    "Call 555\t010\t0100",
    "Call 555_010_0100",
    "Call 555\u2022010\u20220100",
    "Call 555\u2800010\u28000100",
    "Call 18005550100",
    "Call 5550-10-01",
    "Call+15550100123",
    "1-800-555-0100",
    "555\u200b0100\u200b123",
    "\uff0b\uff11 \uff15\uff15\uff15 \uff10\uff11\uff10\uff10 \uff10\uff11\uff12",
    "Call \u2460\u2461\u2462\u2463\u2464\u2465\u2466",
    "+\u0662\u0660 \u0661\u0660\u0660 \u0661\u0662\u0663 \u0664\u0665\u0666\u0667",
)
"""Numbers to call, in groups or not, in other digits, split by invisible marks."""
PLAIN: Final = (
    "v1.2",
    "Q3 report",
    "readers",
    "",
    "Not this one",
    "Ticket 42",
    "Ticket INC0012345",
    "GB29NWBK60161331926819",
    "Duplicate of the 2026-11-17 posting",
    "on 17.11.2026 at 10:30",
    "Duplicate of 2026/11/17",
    "Amount 1250000.00 is over the limit",
    "Wrong amount: 1,250,000.00",
    "invoice.pdf",
    "Mr.Smith",
    "e.g. this, i.e. that",
    "U.S.A.",
    "Ph.D.",
    "v1.10",
    "version 1.2.3",
    "accounts.add_user",
    "finance.readers",
    "Node.js",
    "col\u00b7legi",
    "price @ 5",
    "Vienna (AT) team",
    "Hotel: Hilton",
    "SMS: not delivered",
    "Call 911",
    "O'Brien-Smith",
    "Z\u00fcrich team",
    "\u0644\u0627 \u064a\u0648\u062c\u062f",
    "\u0661\u0660\u0660 \u062c\u0646\u064a\u0647",
)
"""Reasons and names that hold no link: versions, codes, dates, amounts, files."""


@pytest.mark.parametrize("text", LINKS + NUMBERS)
def test_a_link_an_address_or_a_number_is_read_as_a_person_reads_it(text: str) -> None:
    """Full-width, look-alike, and written-out dots; invisible marks; other scripts."""
    assert holds_link(text)


@pytest.mark.parametrize("text", PLAIN)
def test_text_without_one_holds_no_link(text: str) -> None:
    """A version, a code touching its letters, a date, an amount, a file's name."""
    assert not holds_link(text)


def test_typed_text_with_a_link_is_refused_as_the_setting_says(
    use_auth: UseAuth,
) -> None:
    """Shown as typed by default; refused when asked, as a value or an item.

    Text not marked ``Typed``, and the name of what the act is on, never
    is: a name stored was checked where it was typed (user report A17).
    """
    use_auth()
    typed = {"Reason": Typed("Call +1 555 010 0100"), "Adds": ("a", Typed("x.com"))}
    shown = laid_out(typed)
    check_typed("x.com")
    use_auth(AUTHORIZATIONS={"TYPED_LINKS": "refused"})

    assert shown == [("Reason", "Call +1 555 010 0100"), ("Adds", "a, x.com")]
    for details in ({"Reason": typed["Reason"]}, {"Adds": typed["Adds"]}):
        with pytest.raises(LinkNotShown):
            laid_out(details, whole=False)
    assert laid_out({"Reason": "x.com", "User": named("ann@example.com", 1)}) == [
        ("Reason", "x.com"),
        ("User", "ann@example.com (1)"),
    ]
    assert laid_out({"Reason": Typed("Ticket 42")}) == [("Reason", "Ticket 42")]
    with pytest.raises(LinkNotShown):
        check_typed("x.com")
    assert LinkNotShown().code == "link_not_shown"
    assert status_code_for(LinkNotShown()) == 422


def test_a_project_checks_what_was_typed_its_own_way(use_auth: UseAuth) -> None:
    """``on_typed_text`` gets each typed text, as typed, whatever the setting."""
    use_auth()
    seen: list[object] = []

    def no_shouting(text: str) -> None:
        seen.append(text)
        if text.isupper():
            raise TooMuchToShow

    on_typed_text(no_shouting, domain="auth")
    calm = laid_out({"Reason": Typed("Ticket 42"), "Note": "LOUD"})
    with pytest.raises(TooMuchToShow):
        laid_out({"Adds": ("a", Typed("LOUD"))})

    assert calm == [("Reason", "Ticket 42"), ("Note", "LOUD")]
    assert seen == ["Ticket 42", "LOUD"]
    assert all(type(each) is str for each in seen)
