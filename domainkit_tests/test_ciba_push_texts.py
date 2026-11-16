"""Each kind of request is pushed with words of its own (user report I8).

Every push said "Approve sign-in?": a step-up, a transaction, and an act
too. Each kind now has its own title and body, translated by domainkit's
catalogs; ``PUSH_TEXTS`` names a kind's, and a ``PUSH_TITLE`` or
``PUSH_BODY`` a project set still stands for every kind it names none for.
"""

import pytest
from pydantic import ValidationError

from domainkit.contrib.auth import User
from domainkit.contrib.ciba.services import PUSH_DEFAULTS, _push_text
from domainkit.contrib.ciba.settings import CibaSettings
from domainkit.i18n import gettext, override
from domainkit_tests.test_ciba import PHONE, App, Phone, app, sign_in_pushes, start_push
from domainkit_tests.test_ciba_authorizations import Session, acting
from domainkit_tests.test_ciba_step_up import FREE_TEXT_STEP_UP, ask, transaction_of

__all__ = ["acting", "app"]  # The fixtures the tests take.

SIGN_IN = ("Approve sign-in?", "Open the app to approve or deny it.")
STEP_UP = ("Confirm it's you?", "Open the app to see what for, and approve or deny it.")
TRANSACTION = (
    "Approve a transaction?",
    "Open the app to see its amount and payee, and approve or deny it.",
)
ACT = ("Approve an action?", "Open the app to see what it is, and approve or deny it.")
IN_ARABIC = {
    "sign_in": ("هل توافق على تسجيل الدخول؟", "افتح التطبيق لتوافق عليه أو ترفضه."),
    "step_up": (
        "هل تؤكد هويتك؟",
        "افتح التطبيق لترى الغرض منه، ثم توافق عليه أو ترفضه.",
    ),
    "transaction": (
        "هل توافق على المعاملة؟",
        "افتح التطبيق لترى مبلغها والمستفيد منها، ثم توافق عليها أو ترفضها.",
    ),
    "authorization": (
        "هل توافق على الإجراء؟",
        "افتح التطبيق لترى ما هو، ثم توافق عليه أو ترفضه.",
    ),
}


def pushed_texts() -> list[tuple[str, str]]:
    """The title and body of each sign-in push queued, oldest first."""
    return [(push["title"], push["body"]) for push in sign_in_pushes()]


def ask_each_kind(web: App, phone: Phone, username: str = "alice") -> None:
    """A sign-in, a step-up, a transaction, and an act of ``username``'s."""
    client = web()
    session = Session(client, username)
    start_push(client, username)
    assert ask(client, session.headers).status_code == 200
    challenge = transaction_of(phone.user_id)
    asked = client.post(
        f"/auth/app-sign-in/transactions/{challenge}", headers=session.headers
    )
    assert asked.status_code == 200, asked.json()
    assert session.ask_phone(session.asked()["id"]).status_code == 200


@FREE_TEXT_STEP_UP
def test_each_kind_of_request_is_pushed_with_its_own_words(acting: App) -> None:
    """Four kinds, four titles; the data still names the request alone."""
    phone = Phone(acting(PHONE))
    acting(MAX_PENDING=5)

    ask_each_kind(acting, phone)

    assert pushed_texts() == [SIGN_IN, STEP_UP, TRANSACTION, ACT]
    assert [set(push["data"]) for push in sign_in_pushes()] == [
        {"type", "request_id"}
    ] * 4


@FREE_TEXT_STEP_UP
def test_the_words_are_in_the_users_language(
    acting: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped catalogs have each kind's words in Arabic."""
    monkeypatch.setattr(User, "LANGUAGE_FIELD", "last_name")
    phone = Phone(acting(PHONE), "rana", last_name="ar")
    acting(MAX_PENDING=5)

    ask_each_kind(acting, phone, "rana")

    assert pushed_texts() == [
        IN_ARABIC[kind]
        for kind in ("sign_in", "step_up", "transaction", "authorization")
    ]
    with override("ar"):
        assert {
            kind: (gettext(title), gettext(body))
            for kind, (title, body) in PUSH_DEFAULTS.items()
        } == IN_ARABIC


@FREE_TEXT_STEP_UP
def test_a_projects_push_title_and_body_still_stand_for_every_kind(
    acting: App,
) -> None:
    """Set before 2.5, they keep reaching every kind; ``PUSH_TEXTS`` comes first."""
    phone = Phone(acting(PHONE))
    acting(
        MAX_PENDING=5,
        PUSH_TITLE="Approve?",
        PUSH_TEXTS={"authorization": {"TITLE": "Approve this act?"}},
    )

    ask_each_kind(acting, phone)

    assert pushed_texts() == [
        ("Approve?", SIGN_IN[1]),
        ("Approve?", STEP_UP[1]),
        ("Approve?", TRANSACTION[1]),
        ("Approve this act?", ACT[1]),
    ]


def test_a_kinds_words_come_from_its_entry_then_the_setting_then_its_own() -> None:
    """Field by field: ``PUSH_TEXTS[kind]``, then ``PUSH_TITLE``, then the kind's."""
    config = CibaSettings(
        PUSH_BODY="Open the app.",
        PUSH_TEXTS={
            "transaction": {"TITLE": "Pay?", "BODY": "See it in the app."},
            "step_up": {"TITLE": "You?"},
        },
    )

    assert _push_text("transaction", config) == ("Pay?", "See it in the app.")
    assert _push_text("step_up", config) == ("You?", "Open the app.")
    assert _push_text("sign_in", config) == ("Approve sign-in?", "Open the app.")
    assert _push_text("authorization", CibaSettings()) == ACT
    assert _push_text("a_kind_of_its_own", CibaSettings()) == SIGN_IN


@pytest.mark.parametrize(
    "texts",
    [
        {"device": {"TITLE": "Bind?"}},  # Never pushed: scanned.
        {"sign_in": {"TITLE": ""}},
        {"sign_in": {"BODY": "x" * 201}},
        {"sign_in": {"SUBTITLE": "x"}},
    ],
)
def test_push_texts_name_a_pushed_kind_and_bounded_words(
    texts: dict[str, dict[str, str]],
) -> None:
    """Unknown kinds or keys, empty or overlong words: refused when read."""
    with pytest.raises(ValidationError):
        CibaSettings(PUSH_TEXTS=texts)
