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

import copy
import pickle
import time
from decimal import Decimal
from typing import Final
from uuid import UUID

import pytest

from domainkit.contrib.approvals.authorizing import details_of
from domainkit.contrib.auth.actions import Shown
from domainkit.contrib.auth.integration import group_item
from domainkit.contrib.auth.screens import (
    Details,
    LinkNotShown,
    TooMuchToShow,
    Typed,
    TypedName,
    _top_level_domains,
    check_typed,
    fitted,
    holds_link,
    laid_out,
    named,
    on_typed_text,
    shows_link,
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
    "evil\u2022com",
    "evil\ua78fcom",
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
    "Call support, then open secure-login . com",
    "verify at secure-login .com",
    "verify at secure-login. com",
    "secure-login dot com",
    "write to help at secure-login dot com",
    "evil DOT Com",
    "evil. COM",
    "evil . org/login",
    "visit evil . co . uk",
    "evil\u3000.com",
    "evil\u00a0.\u00a0com",
    "203 . 0 . 113 . 7",
    "https : //evil",
    "hxxps[:] //evil",
    "evil<dot>com",
    "ann <at> example",
    "secure-login (dot) com",
    "secure-login ( dot ) com",
    "secure-login (.) com",
    "help (at) secure-login (dot) com",
    "secure-login    .    com",
    "secure-login     dot     com",
    "secure-login\n.\ncom",
    "secure-login\n. com",
    "secure-login .\ncom",
    "secure-login dot\ncom",
    "evil\n.com",
    "example    [.]    com",
    "evil.com.xy",
    "foo.bar.baz",
    "evil.com-login",
    "evil.com\u00b7xy",
    "secure-login.com.verify-account.xy",
    "evil.co-uk",
    "evil . com . xy",
    "evil .com.xy",
    "evil . Com",
    "evil. Com",
    "evil \u0660 com",
    "evil\u0660 com",
    "evil \u0660com",
    "evil \u06f0 com",
    "Renew at evil \u0660 com",
)
"""Links, as written and as written to pass for something else; since 2.10,
a name inside a name, ``Com`` after a spaced dot, an Arabic-Indic zero spaced
out between two names."""
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
    "Call 612 345 678",
    "Call 5550100123 all day",
    "call 555 010 0123 EUR",
    "Call (555) 010-0123 USD",
    "EUR 555 01 0123",
    "612.345.678",
    "+34 612.345.678",
    "Call-555-010-0123",
    "call_555_010_0123",
    "Locked out? call-5550100123",
    "phone-5550100123",
    "support-555-010-0123",
    "$ 555 010 0123",
    "555 010 0123 \u20ac",
    "\u20ac555-010-0123",
    "call 5550100123 EGP",
    "Call 1.555.010.012",
    "555.010.012.3",
    "1,555,010,0123",
    "JIRA-5550100123",
    "tel5550100123",
    "555 010 0123,00",
    "Call 612,345,678",
    "\u0441all 5,550,100,123",
    "\u0442el 5,550,100,123",
    "\U0001f4de 5,550,100,123",
    "\u260e 5,550,100,123",
    "\u260e\ufe0f 5,550,100,123",
    "\U0001f4f1 5,550,100,123",
    "\u0627\u062a\u0635\u0644 612,345,678",
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
    "1.234.567,89",
    "amount 1.250.000,00",
    "1 234 567,89",
    "12 500 000 EUR",
    "EUR 12 500 000",
    "$1 250 000",
    "Amount 1,250,000 is over the limit",
    "5,550,100,123",
    "Invoices 1001, 1002 and 1003",
    "CVE-2024-12345",
    "REQ-2026-0042",
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
    "\u0662\u0660\u0662\u0666",
    "\u0660\u0661\u0660\u0660",
    "\u0627\u0644\u0631\u0635\u064a\u062f \u0660 \u062c\u0646\u064a\u0647",
    "evil.com_login",
)
"""Reasons and names that hold no link: versions, codes, dates, amounts, files."""
PROSE: Final = (
    "Approved. It is fine",
    "Checked. Me too",
    "Done. Info sent to the team",
    "ok. go ahead",
    "Paid. Net 30 terms",
    "Customer's email is missing a dot in the domain",
    "the dot next to the name",
    "on the dot at nine",
    "Mr. Smith",
    "No. 5 in the queue",
    "wait . what",
    "Ticket 42 . Closed",
    "time: 10:30 // see notes",
    "Gross vs. net amounts differ",
    "Refund approved, i.e. company credit only",
    "See the attached, e.g. organisation chart",
    "Expense exceeds the limit, incl. network costs",
    "Paid in full, approx. commission withheld",
    "Rejected: duplicate, etc. complete the form again",
    "Approved. compliance checked",
    "Done. network restored",
    "Hiring a dot net developer",
    "Rebuild the .NET service",
    "Attach the .zip file",
    "Use a .app bundle",
    "Wrong . Please resend",
    "Total 10 . Thanks",
    "invoice no. 5 net of tax",
    "Approved per dept. org policy",
    "Support case 1234 . Resolved",
    "Rejected . No receipt",
    "Sent to team . In review",
    "Called customer . To follow up",
    "Customer said yes . Me too",
    "Please call . At home",
    "Use a period (.) at the end",
)
"""Sentences whose next word, after a full stop, a dot, or ``dot``, is a
top-level domain or starts with one (since 2.9): ``Done. Info sent``, ``vs.
net``, ``i.e. company``, a stray space (``Rejected . No receipt``)."""
READ_AS_LINKS: Final = (
    "notes.md",
    "Q3.report",
    "team.legal",
    "GB29 NWBK 6016 1331 9268 19",
    "100-0001",
    "01310-100",
    "build 1234567",
    "1.234.567",
    "612 345 678",
    "EUR 1250000",
    "$1250000",
    "JIRA-1234567",
    "user_1234567",
    "Amount 1.250.000,00; I will call you",
    "the dot com era",
    "fine . me too",
    "the key (dot) is broken",
    "report.final.docx",
    "\u062a\u0645\u062a \u0625\u0636\u0627\u0641\u0629 \u0660 \u0645\u0648\u0642\u0639",
)
"""Text that holds no link, read as one: a top-level domain after a dot, seven
digits or more apart by spaces, dots or hyphens, or not grouped, an amount or a
code beside a word that asks for a call (ADR 0180's false positives); ``com``
after ``dot``, a top-level domain in lower case after a dot spaced on both
sides, and one after ``(dot)`` (since 2.9); a file named with a top-level
domain before its extension, which a phone links, and an Arabic-Indic zero
spaced out between two words, the second a top-level domain (since 2.10)."""
DOTTED: Final = (
    "Upgraded the service to version 2.10.1 today",
    "Rolled back from v3.2.0-rc.1 to v3.1.4",
    "Python 3.13.1 and Django 5.1.4 on the build host",
    "Wait for v2.0-beta.3 before the change",
    "Section 4.2.1 of the policy applies",
    "Rule 7.3.2(b) applies here",
    "node v20.11.1-alpine image",
    "See report.pdf and notes.txt.bak attached",
    "Attached: invoice_2026.11.pdf, summary.docx",
    "archive.tar.gz restored from backup",
    "data.2026-11-23.csv was imported",
    "logo.v2.png and photo.2026.11.jpg",
    "minutes.draft.docx circulated",
    "backup.sql.gz from last night",
    "statement.2026-11.pdf for the audit",
    "contract-final.v3.pdf signed",
    "payroll_nov.xlsx.enc sent",
    "app.config.json was edited",
    "index.min.js and styles.min.css",
    "Approved, e.g. for travel, i.e. flights only",
    "Acme Ltd. and Beta Inc. signed",
    "Dr. Ahmed, Ph.D., M.D. approved",
    "U.S.-based vendors only",
    "St. Mary's ward, No. 4",
    "a.k.a. the night shift",
    "pp. 12-15 of vol. 2",
    "approx. 2.5x faster",
    "Accounts dept.-wide change",
    "Sat. 23 Nov. at 10 a.m.",
    "Co. Ltd. registration",
    "Messrs. Khan & Co.",
    "Meeting at 10.30 a.m. on 23.11.2026",
    "Rate is 3.75% for 12.5 months",
    "Totals: 1,234.56 and 7.5",
    "the 1.5-2.0 range",
    "Due 2026.11.30, paid 2026.12.01",
    "Temperature 36.6-37.2",
    "Done.Thanks",
    "Checked.All good",
    (
        "\u062a\u0645 \u0627\u0644\u062a\u062d\u062f\u064a\u062b \u0625\u0644\u0649 "
        "\u0627\u0644\u0625\u0635\u062f\u0627\u0631 2.10.1"
    ),
    "\u0627\u0644\u0645\u0644\u0641 report.pdf \u0645\u0631\u0641\u0642",
    (
        "\u0628\u062a\u0627\u0631\u064a\u062e "
        "\u0662\u0663/\u0661\u0661/\u0662\u0660\u0662\u0666"
    ),
    (
        "\u0627\u0644\u0645\u0628\u0644\u063a "
        "\u0661\u0662\u0665\u0660\u066b\u0665\u0660 \u062c\u0646\u064a\u0647"
    ),
    "\u0634\u0631\u0643\u0629 \u0627\u0644\u0646\u0648\u0631 \u0630.\u0645.\u0645",
    "\u062f. \u0623\u062d\u0645\u062f\u060c \u0645. \u0645\u062d\u0645\u062f",
    "\u0627\u0644\u0633\u0627\u0639\u0629 \u0661\u0660:\u0663\u0660 \u0635",
    "\u0627\u0644\u0646\u0633\u0628\u0629 \u0663\u066b\u0667\u0665\u066a",
    (
        "\u0627\u0644\u0628\u0646\u062f \u0664.\u0662.\u0661 \u0645\u0646 "
        "\u0627\u0644\u0644\u0627\u0626\u062d\u0629"
    ),
    (
        "\u062a\u0645\u062a \u0627\u0644\u0645\u0648\u0627\u0641\u0642\u0629. "
        "\u0634\u0643\u0631\u0627\u064b"
    ),
    "\u0645\u0644\u0627\u062d\u0638\u0627\u062a.txt.bak",
    (
        "\u0639\u062f\u062f \u0627\u0644\u0645\u062d\u0627\u0648\u0644\u0627\u062a "
        "\u0660 \u0627\u0644\u064a\u0648\u0645"
    ),
    "\u0627\u0644\u062e\u0635\u0645 \u0660 \u0631\u064a\u0627\u0644",
    "\u0634.\u0645.\u0645 \u0627\u0644\u0642\u0627\u0647\u0631\u0629",
    "\u062a\u0642\u0631\u064a\u0631.\u0646\u0647\u0627\u0626\u064a.pdf",
)
"""Notes in English and Arabic with dots inside words: versions, files,
abbreviations, decimals, dates, a zero as a count. None holds a top-level
domain inside a name (since 2.10)."""


@pytest.mark.parametrize("text", LINKS + NUMBERS)
def test_a_link_an_address_or_a_number_is_read_as_a_person_reads_it(text: str) -> None:
    """Full-width, look-alike, and written-out dots; invisible marks; other scripts."""
    assert holds_link(text)


@pytest.mark.parametrize("text", PLAIN + PROSE)
def test_text_without_one_holds_no_link(text: str) -> None:
    """A version, a code touching its letters, a date, an amount, a file's name."""
    assert not holds_link(text)


@pytest.mark.parametrize("text", READ_AS_LINKS)
def test_text_a_reader_takes_for_a_link_is_read_as_one(text: str) -> None:
    """The false positives the docs name: write them otherwise."""
    assert holds_link(text)


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


def test_a_name_spelled_out_is_refused_where_typed(use_auth: UseAuth) -> None:
    """Spaced out, or ``dot`` for its dot, it reaches no screen (audit A28-SEC-05)."""
    use_auth(AUTHORIZATIONS={"TYPED_LINKS": "refused"})
    spelled = (
        "Call support, then open secure-login . com",
        "verify at secure-login .com",
        "verify at secure-login. com",
        "secure-login dot com",
        "write to help at secure-login dot com",
    )

    for text in spelled:
        with pytest.raises(LinkNotShown):
            check_typed(text)
        with pytest.raises(LinkNotShown):
            laid_out({"Reason": Typed(text)})
    check_typed("Approved. It is fine; info sent to the team")


@pytest.mark.parametrize("text", PROSE)
def test_prose_is_shown_whatever_a_screen_cuts_of_it(
    use_auth: UseAuth, text: str
) -> None:
    """Cuts are read without spelled-out names: ``i.e. company`` cut to ``i.e. com…``.

    ``check_typed`` checks each cut from the twentieth character, as a screen
    may show it; the lead review found prose refused by its cuts alone. A cut
    of a dotted name or of a number may still read as a link on its own
    (``finance.re…``, ADR 0180), so ``PLAIN`` is not read this way.
    """
    use_auth(AUTHORIZATIONS={"TYPED_LINKS": "refused"})

    check_typed(f"Reviewed by the desk: {text}")
    assert laid_out({"Reason": Typed(f"Reviewed by the desk: {text}")}, whole=False)


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


def test_a_typed_line_is_checked_as_it_is_shown(use_auth: UseAuth) -> None:
    """Cut, escaped, or after a list's first items, a line can read as a link.

    ``evil.corporate`` names no domain; cut to ``evil.co…`` it does, and
    a phone links it. A code's letters cut off leave a number to call; a
    new line shown as a backslash and ``n`` makes ``n.com``. Items joined
    with a comma never make one (review of ADR 0180).
    """
    use_auth()
    domain = "Approved; details at our partner evil.co"
    domain = domain.rjust(63, "x") + "rporate"
    number = "Closed. For any question please ring 555 0100 123".rjust(63, "x") + "abc"
    first = ("z" * 44 + " evil.co").ljust(52, "z") + "rporate here"
    cuts: list[Details] = [
        {"Reason": Typed(domain)},
        {"Name": Typed(number)},
        {"Adds": (Typed(domain), "b")},
        {**{f"k{each}": "v" for each in range(5)}, "Adds": (Typed(first), "b")},
    ]
    shown = [dict(laid_out(details, whole=False)) for details in cuts]
    use_auth(AUTHORIZATIONS={"TYPED_LINKS": "refused"})

    assert not any(map(holds_link, (domain, number, first, "\n.com")))
    assert shown[0]["Reason"].endswith(" evil.co…")
    assert shown[1]["Name"].endswith(" 123…")
    assert shown[2]["Adds"].endswith(" evil.co…")
    assert shown[3]["Adds"].endswith(" evil.co… and 1 more")
    for details in cuts:
        with pytest.raises(LinkNotShown):
            laid_out(details, whole=False)
    with pytest.raises(LinkNotShown):
        laid_out({"Reason": Typed("\n.com")})
    assert laid_out({"Adds": (Typed("555 010"), Typed("0100"))}) == [
        ("Adds", "555 010, 0100")
    ]
    assert laid_out({"Reason": "x" * 63 + "abc"}, whole=False) == [
        ("Reason", "x" * 63 + "…")
    ]


def test_a_name_shown_later_is_checked_as_a_screen_may_cut_it(
    use_auth: UseAuth,
) -> None:
    """From its twentieth character, each cut, as ``named`` and lists cut it."""
    use_auth(AUTHORIZATIONS={"TYPED_LINKS": "refused"})

    check_typed("finance.readers")
    check_typed("evil.corporate")
    with pytest.raises(LinkNotShown):
        check_typed("a team for the vendor evil.corporate")


@pytest.mark.parametrize("text", DOTTED)
def test_dots_inside_words_in_prose_hold_no_link(text: str) -> None:
    """A top-level domain inside a name is read; prose's dots are not (user report A17).

    Pins what passes since 2.10, when a top-level domain ends a name before
    a dot or a hyphen too: a version, a file, an abbreviation, a decimal, a
    date, in English and in Arabic, whole and on a screen's line.
    """
    assert not holds_link(text)
    assert not shows_link(text)


def test_a_top_level_domain_ends_a_name_where_a_linker_ends_a_word() -> None:
    """Before a dot, a hyphen, or a middle dot, as at its end (user report A17).

    A phone links ``evil.com`` in each. An underscore joins a word, as a
    linker reads one; an IDN's A-label holds its hyphens, and no other
    top-level domain holds one.
    """
    ended = ("evil.com.xy", "evil.com-login", "evil.com\u00b7xy", "evil.com-")
    joined = ("evil.com_login", "evil.comx.xy", "evil.xcom-login", "evil-com.xy")
    hyphened = {each for each in _top_level_domains() if "-" in each}

    assert all(map(holds_link, ended))
    assert not any(map(holds_link, joined))
    assert holds_link("x.xn--p1ai")
    assert hyphened
    assert all(each.startswith("xn--") for each in hyphened)


def test_a_label_too_long_for_a_domain_is_not_read_for_one() -> None:
    """Past 63 characters (RFC 1035), in any script: read once, never as a domain."""
    assert not holds_link("x." + "a" * 64)
    assert not holds_link("x." + "\u0436" * 64)
    assert holds_link("x." + "a" * 63 + ".com")


def test_a_long_run_of_white_space_is_read_once() -> None:
    """Once, not again from each of its spaces: 2.9 took seconds on 50,000."""
    text = "a" + " " * 50_000 + "b"
    start = time.perf_counter()

    assert not holds_link(text)
    assert holds_link("evil" + " " * 50_000 + "[.] com")
    assert time.perf_counter() - start < 5


def test_a_screen_shows_a_link_as_laid_out_reads_typed_text(use_auth: UseAuth) -> None:
    """Whole with the spaced readings, its line without them (user report A17).

    A project reads stored text a screen shows so. Read with the spaced
    readings, a cut makes prose a name: ``approved. commercial`` cut to
    ``approved. com…`` holds one. ``shows_link`` says what ``laid_out``
    refuses of a ``Typed`` text on a line, ``length`` its line's.
    """
    use_auth(AUTHORIZATIONS={"TYPED_LINKS": "refused"})
    prose = [
        f"Reviewed, {text}".rjust(63, "x") + f"{rest} terms apply"
        for text, rest in (
            ("approved. com", "mercial"),
            ("i.e. com", "pany"),
            ("Ltd. com", "pliance"),
            ("doc .com", "pressed"),
        )
    ]
    cut = "Approved; details at our partner evil.co".rjust(63, "x") + "rporate"

    def refused(text: str) -> bool:
        try:
            laid_out({"Note": Typed(text)}, whole=False)
        except LinkNotShown:
            return True
        return False

    assert all(fitted(text).endswith("com…") for text in prose)
    assert all(holds_link(fitted(text)) for text in prose)
    assert not any(map(shows_link, prose))
    assert shows_link(cut)
    assert not holds_link(cut)
    assert shows_link("visit evil . com")
    assert shows_link("\n.com")
    assert not holds_link("\n.com")
    assert shows_link("Approved; see evil.corporate", 40) is False
    assert shows_link("Approved; see our partner evil.corporate", 34)
    for text in (*prose, cut, *LINKS, *NUMBERS[:10], *PROSE, *DOTTED):
        assert shows_link(text) == refused(text), text


def test_digits_are_a_number_to_call_only_where_asked_without_numbers() -> None:
    """A licence's number is no number to call; a word that asks for one makes it one.

    ``numbers=False`` reads digits as an amount or a code is read: a
    number to call with a plus sign, a bracket, or beside a word that asks
    for a call, as a person reads it (user report A17).
    """
    licences = ("No. 123 456 789", "File 2026/0045", "Licence 1234567")
    calls = (
        "Call 123 456 789",
        "WhatsApp 1234567",
        "\u0441all 1234567",
        "\u260e 1234567",
        "\u0627\u062a\u0635\u0644 1234567",
        "+1 555 010 0100",
        "(555) 010-0100",
    )
    links = ("evil.com", "ann@example.com", "203.0.113.7", "tel:+15550100")

    assert all(map(holds_link, licences))
    assert not any(holds_link(text, numbers=False) for text in licences)
    assert not any(shows_link(text, numbers=False) for text in licences)
    assert all(holds_link(text, numbers=False) for text in (*calls, *links))
    coded = "Closed; ring 555 0100 123".rjust(63, "x") + "abc"
    assert not holds_link(coded)
    assert shows_link(coded)
    assert not shows_link(coded, numbers=False)


GROUP_ID: Final = UUID("12345678-1234-5678-1234-567812341234")
"""A group's id whose item ends in four digits: ``(…1234)``."""


def test_a_group_added_shows_its_stored_name_typed(use_auth: UseAuth) -> None:
    """A name that holds a link is refused where it is granted (user report A17).

    Whole, or as its item shows it cut (``evil.co…``); with
    ``TYPED_LINKS`` shown, as in 2.9. The id and the counts beside it are
    never read: a hundred permissions beside ``(…1234)`` would read as a
    number to call. A rejection's screen drops the mark, as for any typed
    text of a request.
    """
    use_auth()
    linked = group_item("visit evil.example", GROUP_ID, 3)
    many = group_item("readers", GROUP_ID, 100)
    cut = next(
        item
        for width in range(40)
        if (item := group_item("x" * width + " evil.corporate", GROUP_ID, 2, 1))
        .rpartition(" (")[0]
        .endswith(" evil.co…")
    )
    shown = laid_out({"Adds": [linked, many, cut]})
    use_auth(AUTHORIZATIONS={"TYPED_LINKS": "refused"})

    assert isinstance(linked, TypedName)
    assert isinstance(cut, TypedName)
    assert (linked, linked.name, linked.shown) == (
        "visit evil.example (…1234) 3 perms",
        "visit evil.example",
        "visit evil.example",
    )
    assert shown[0] == ("Adds", f"{linked}, {many}")
    assert not holds_link(cut.name)
    for details in ({"Adds": [many, linked]}, {"Adds": [cut]}, {"Group": linked}):
        with pytest.raises(LinkNotShown):
            laid_out(details)
    assert holds_link(many)
    assert laid_out({"Adds": [many]}) == [("Adds", "readers (…1234) 100 perms")]
    with pytest.raises(LinkNotShown):
        laid_out({"Adds": [many, Typed("x")]})  # Read as shown, beside typed text.
    assert details_of("auth.set_groups", [("Adds", [linked])], typed=False)


def test_a_project_checks_a_stored_group_name_its_own_way(use_auth: UseAuth) -> None:
    """``on_typed_text`` gets the name as stored, never the id or the counts."""
    use_auth()
    seen: list[str] = []
    on_typed_text(seen.append, domain="auth")
    long = "readers of the finance department, cairo office, second floor"
    item = group_item(long, GROUP_ID, 2)

    laid_out({"Adds": [item, group_item("clerks", 7, 0)]})

    assert isinstance(item, TypedName)
    assert seen == [long, "clerks"]
    assert all(type(each) is str for each in seen)
    assert item.shown.endswith("…")
    assert item == f"{item.shown} (…1234) 2 perms"


def test_a_group_item_copies_with_its_name() -> None:
    """A copy, or a pickle, keeps the name and how it shows: the screen reads them."""
    item = TypedName("readers (7) 1 perm", "readers", "readers")

    for each in (copy.deepcopy(item), pickle.loads(pickle.dumps(item))):  # noqa: S301
        assert (each, each.name, each.shown) == (item, item.name, item.shown)
        assert isinstance(each, TypedName)
