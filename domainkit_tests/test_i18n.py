"""Translations: the active language, negotiation, catalogs, and the middleware."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.datastructures import MutableHeaders
from starlette.types import Message, Receive, Scope, Send

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import Settings, override_settings
from domainkit.http import get_asgi_application
from domainkit.http.locale import LocaleMiddleware
from domainkit.i18n import (
    LazyString,
    get_language,
    gettext,
    gettext_lazy,
    gettext_noop,
    negotiate,
    ngettext,
    ngettext_lazy,
    npgettext,
    override,
    pgettext,
    pgettext_lazy,
    validation_message,
)
from domainkit.i18n.catalogs import compile_po
from domainkit.i18n.translation import (
    LOCALE_DIR,
    MAX_LANGUAGE_RANGES,
    catalog,
    check_language,
    locale_dirs,
    reset_catalogs,
    to_locale,
)
from domainkit.i18n.validation import _localized
from domainkit.utils.headers import patch_vary
from domainkit_tests.conftest import ALPHA, MakePackage

ARABIC_PLURALS: Final = (
    "nplurals=6; plural=n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : "
    "n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5;"
)
MIDDLEWARE: Final = [
    "domainkit.http.locale.LocaleMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]


@pytest.fixture(autouse=True)
def fresh_catalogs() -> Iterator[None]:
    """Read catalogs afresh in every test."""
    reset_catalogs()
    yield
    reset_catalogs()


def write_catalog(
    directory: Path,
    locale: str,
    entries: str,
    plurals: str = "nplurals=2; plural=(n != 1);",
) -> Path:
    """Write and compile ``<directory>/<locale>/LC_MESSAGES/messages.po``."""
    po = directory / locale / "LC_MESSAGES" / "messages.po"
    po.parent.mkdir(parents=True)
    po.write_text(
        'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        f'"Plural-Forms: {plurals}\\n"\n\n{entries}',
        encoding="utf-8",
    )
    po.with_suffix(".mo").write_bytes(compile_po(po.read_text(encoding="utf-8")))
    return directory


# The active language -----------------------------------------------------------------


def test_the_language_is_the_settings_until_overridden() -> None:
    """``LANGUAGE_CODE`` outside any block; ``override`` nests and restores."""
    assert get_language() == "en"
    with override("ar"):
        assert get_language() == "ar"
        with override("pt-br"):
            assert get_language() == "pt-br"
        assert get_language() == "ar"
    assert get_language() == "en"
    with override_settings(LANGUAGE_CODE="ar"):
        assert get_language() == "ar"


@pytest.mark.parametrize("bad", ["", "EN", "e", "english", "../etc", "ar_EG", "ar-"])
def test_languages_are_codes(bad: str) -> None:
    """Only lower-case codes are languages: they become catalog paths."""
    with pytest.raises(ValueError, match="not a language code"), override(bad):
        pass
    with pytest.raises(ValueError, match="not a language code"):
        check_language(bad)
    assert check_language("zh-hant") == "zh-hant"


@pytest.mark.parametrize(
    ("language", "locale"),
    [
        ("ar", "ar"),
        ("pt-br", "pt_BR"),
        ("en-us", "en_US"),
        ("zh-hant", "zh_Hant"),
        ("es-419", "es_419"),
        ("sr-latn-rs", "sr_Latn-rs"),
        ("xx-abc", "xx_Abc"),
    ],
)
def test_catalogs_are_filed_under_locales(language: str, locale: str) -> None:
    """Regions are upper case, scripts title case, as gettext and Django file them."""
    assert to_locale(language) == locale


# Negotiation -------------------------------------------------------------------------

LANGUAGES: Final = ("en", "ar", "pt-br", "fr-ca")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, "en"),
        ("", "en"),
        ("ar", "ar"),
        ("AR", "ar"),
        (" ar-EG , en;q=0.5", "ar"),
        ("de, ar;q=0.1", "ar"),
        ("en;q=0.8, ar", "ar"),
        ("en;q=0.8, ar;q=0.9", "ar"),
        ("ar;q=0.5, en;q=0.5", "ar"),
        ("en;q=0.5, ar;q=0.5", "en"),
        ("ar;q=0, en;q=0.1", "en"),
        ("ar;q=0.000", "en"),
        ("ar;q=0.001", "ar"),
        ("ar;q=1.000, en", "ar"),
        ("ar;q=1.001, fr-ca;q=0.2", "fr-ca"),
        ("ar;q=2, fr-ca;q=0.2", "fr-ca"),
        ("ar;q=.5, fr-ca;q=0.2", "fr-ca"),
        ("ar;q=0.5000, fr-ca;q=0.2", "fr-ca"),
        ("ar;Q=0.4, en;q=0.3", "ar"),
        ("ar;level=1, en;q=0.9", "ar"),
        ("ar; q = 0.4 , en;q=0.3", "ar"),
        ("pt", "pt-br"),
        ("pt-pt", "pt-br"),
        ("fr", "fr-ca"),
        ("*", "en"),
        ("de, *;q=0.5, ar;q=0.1", "en"),
        ("ar-", "en"),
        ("123, ar;q=0.1", "ar"),
        ("ar-eg-x-y-zzzzzzzzz", "en"),
        ("a" * 9, "en"),
        ("de", "en"),
        ("ar;level=1;q=0.1, en;q=0.5", "en"),  # Other parameters are skipped.
        ("ar;q=0.5=1, en;q=0.1", "en"),  # A malformed quality drops the range.
    ],
)
def test_accept_language_picks_the_most_preferred_language(
    header: str | None, expected: str
) -> None:
    """By quality, then order; region falls back to language, language to a variant."""
    assert negotiate(header, LANGUAGES, "en") == expected


def test_negotiation_reads_a_bounded_header() -> None:
    """Only the first 32 ranges, and 1,024 characters, are read."""
    filler = ["de"] * (MAX_LANGUAGE_RANGES - 1)
    assert negotiate(", ".join([*filler, "ar"]), LANGUAGES, "en") == "ar"
    assert negotiate(", ".join([*filler, "de", "ar"]), LANGUAGES, "en") == "en"
    padding = "de-aaaaaaaa-bbbbbbbb-cccccccc;q=0.1," * 28  # 1,008 characters.
    assert negotiate(f"{padding}ar", LANGUAGES, "en") == "ar"
    assert negotiate(f"{padding}de-aaaaaaaa-bb,ar", LANGUAGES, "en") == "en"


# Catalogs ----------------------------------------------------------------------------


def test_domainkit_ships_arabic() -> None:
    """The framework's messages, and pydantic's templates, in Arabic."""
    with override("ar"):
        assert gettext("The request is invalid.") == "الطلب غير صالح."
        assert pgettext("validation", "Field required") == "هذا الحقل مطلوب"
        assert gettext("Not a message anyone wrote.") == "Not a message anyone wrote."
        assert gettext("") == ""  # gettext's own would be the catalog's header.
    assert gettext("The request is invalid.") == "The request is invalid."
    assert catalog("ar").info()["language"] == "ar"


def test_catalogs_are_looked_up_in_order(
    tmp_path: Path, make_package: MakePackage
) -> None:
    """``LOCALE_PATHS``, then each domain in order, then domainkit's own."""
    project = write_catalog(
        tmp_path / "project",
        "ar",
        'msgid "Invalid token."\nmsgstr "رمز مرفوض."\n',
    )
    first = make_package("words.first", {})
    write_catalog(
        first / "locale",
        "ar",
        'msgid "Invalid token."\nmsgstr "first"\n\n'
        'msgid "Token expired."\nmsgstr "انتهى الرمز."\n',
    )
    second = make_package("words.second", {})
    write_catalog(
        second / "locale",
        "ar",
        'msgid "Token expired."\nmsgstr "second"\n\n'
        'msgid "Domain only."\nmsgstr "للمجال فقط."\n',
    )
    registry.clear()
    domains = [ALPHA, "words.first", "words.second"]
    with override_settings(INSTALLED_DOMAINS=domains, LOCALE_PATHS=[project]):
        setup()
        assert locale_dirs() == (
            project,
            registry.get_domain_config("alpha").path / "locale",
            first / "locale",
            second / "locale",
            LOCALE_DIR,
        )
        with override("ar"):
            assert gettext("Invalid token.") == "رمز مرفوض."
            assert gettext("Token expired.") == "انتهى الرمز."
            assert gettext("Domain only.") == "للمجال فقط."
            assert gettext("Session ended; log in again.") == (
                "انتهت الجلسة؛ سجّل الدخول مرة أخرى."
            )
    registry.clear()


def test_a_region_reads_its_own_catalog_then_its_language(tmp_path: Path) -> None:
    """``pt-br`` looks in ``pt_BR``, then in ``pt``."""
    write_catalog(tmp_path, "pt_BR", 'msgid "Bye"\nmsgstr "Tchau"\n')
    write_catalog(
        tmp_path, "pt", 'msgid "Bye"\nmsgstr "Adeus"\n\nmsgid "Hi"\nmsgstr "Olá"\n'
    )
    with override_settings(LOCALE_PATHS=[tmp_path]):
        with override("pt-br"):
            assert (gettext("Bye"), gettext("Hi")) == ("Tchau", "Olá")
        with override("pt"):
            assert gettext("Bye") == "Adeus"


def test_plurals_follow_the_language_rule(tmp_path: Path) -> None:
    """Arabic has six forms; a message no catalog has follows English's rule."""
    write_catalog(
        tmp_path,
        "ar",
        'msgid "{n} file"\nmsgid_plural "{n} files"\n'
        'msgstr[0] "لا ملفات"\nmsgstr[1] "ملف واحد"\nmsgstr[2] "ملفان"\n'
        'msgstr[3] "{n} ملفات"\nmsgstr[4] "{n} ملفًا"\nmsgstr[5] "{n} ملف"\n\n'
        'msgctxt "drawer"\nmsgid "{n} file"\nmsgid_plural "{n} files"\n'
        'msgstr[0] "لا أدراج"\nmsgstr[1] "درج"\nmsgstr[2] "درجان"\n'
        'msgstr[3] "أدراج"\nmsgstr[4] "درجًا"\nmsgstr[5] "درج"\n',
        plurals=ARABIC_PLURALS,
    )
    with override_settings(LOCALE_PATHS=[tmp_path]), override("ar"):
        forms = [ngettext("{n} file", "{n} files", n) for n in (0, 1, 2, 3, 11, 100)]
        drawers = [npgettext("drawer", "{n} file", "{n} files", n) for n in (2, 103)]
        unknown = [ngettext("an apple", "apples", n) for n in (1, 2)]

    assert forms == [
        "لا ملفات",
        "ملف واحد",
        "ملفان",
        "{n} ملفات",
        "{n} ملفًا",
        "{n} ملف",
    ]
    assert drawers == ["درجان", "أدراج"]
    assert unknown == ["an apple", "apples"]


def test_catalogs_are_read_once_until_reset(tmp_path: Path) -> None:
    """A recompiled catalog is read again only after ``reset_catalogs``."""
    write_catalog(tmp_path, "ar", 'msgid "Hi"\nmsgstr "أهلًا"\n')
    with override_settings(LOCALE_PATHS=[tmp_path]), override("ar"):
        assert gettext("Hi") == "أهلًا"
        mo = tmp_path / "ar" / "LC_MESSAGES" / "messages.mo"
        mo.write_bytes(
            compile_po(
                'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n\n'
                'msgid "Hi"\nmsgstr "مرحبًا"\n'
            )
        )
        assert gettext("Hi") == "أهلًا"
        reset_catalogs()
        assert gettext("Hi") == "مرحبًا"


# Lazy strings ------------------------------------------------------------------------


def test_lazy_strings_translate_when_used() -> None:
    """Defined once, they follow the language active when they are used."""
    invalid = gettext_lazy("The request is invalid.")
    field = pgettext_lazy("validation", "Field required")
    things = ngettext_lazy("{n} thing", "{n} things", 2)

    assert str(invalid) == "The request is invalid."
    assert repr(invalid) == "<LazyString 'The request is invalid.'>"
    assert str(things) == "{n} things"
    with override("ar"):
        assert str(invalid) == "الطلب غير صالح."
        assert str(field) == "هذا الحقل مطلوب"
        assert invalid == "الطلب غير صالح."
        assert invalid == gettext_lazy("The request is invalid.")
        assert invalid != gettext_lazy("Invalid token.")
        assert invalid != 5
        assert hash(invalid) == hash("الطلب غير صالح.")
        assert "الطلب غير صالح." in {invalid}
    assert invalid + "!" == "The request is invalid.!"
    assert "> " + invalid == "> The request is invalid."
    assert LazyString(lambda: "%s and %s") % ("a", "b") == "a and b"
    assert things.format(n=2) == "2 things"
    assert str(ngettext_lazy("{n} thing", "{n} things", 1)) == "{n} thing"
    assert LazyString(lambda: "{} and {}").format("a", "b") == "a and b"
    assert npgettext("drawer", "{n} box", "{n} boxes", 2) == "{n} boxes"
    assert gettext_noop("Not translated now.") == "Not translated now."


# Validation messages -----------------------------------------------------------------


def validation_error(**data: Any) -> dict[str, Any]:  # noqa: ANN401 - any input.
    """The first error pydantic reports for an order made of ``data``."""
    from domainkit_tests.sample_platform_routes import Order

    with pytest.raises(ValidationError) as caught:
        Order.model_validate(data)
    error: dict[str, Any] = dict(caught.value.errors()[0])
    return error


def test_validation_messages_are_translated_from_their_templates() -> None:
    """The template is translated; the context fills it, in the language's digits."""
    too_long = validation_error(reference="abcdefg", quantity=1)
    missing = validation_error(reference="a")
    not_positive = validation_error(reference="a", quantity=0)

    assert validation_message(too_long) == "String should have at most 5 characters"
    with override("ar"):
        assert validation_message(too_long) == ("يجب ألا يزيد عدد الأحرف على \u0665")
        assert validation_message(missing) == "هذا الحقل مطلوب"
        assert validation_message(not_positive) == ("يجب أن تكون القيمة أكبر من \u0660")
        assert validation_message({"type": "value_error", "msg": "x", "ctx": {}}) == "x"


def test_untranslated_and_custom_messages(tmp_path: Path) -> None:
    """Without a translation, pydantic's text; a custom type, by its message."""
    write_catalog(
        tmp_path,
        "ar",
        'msgid "Passwords differ."\nmsgstr "كلمتا المرور مختلفتان."\n\n'
        'msgctxt "validation"\nmsgid "Value error, {error}"\n'
        'msgstr "قيمة مرفوضة: {error}"\n',
    )
    hashable = {"type": "set_item_not_hashable", "msg": "Set items should be hashable"}
    custom = {"type": "password_mismatch", "msg": "Passwords differ."}
    refused = {"type": "string_too_long", "msg": "Too long", "ctx": {"max_length": "x"}}
    value = {
        "type": "value_error",
        "msg": "Value error, Passwords differ.",
        "ctx": {"error": ValueError("Passwords differ.")},
    }
    with override_settings(LOCALE_PATHS=[tmp_path]), override("ar"):
        assert validation_message(hashable) == "Set items should be hashable"
        assert validation_message(custom) == "كلمتا المرور مختلفتان."
        assert validation_message(refused) == "Too long"
        assert validation_message(value) == "قيمة مرفوضة: كلمتا المرور مختلفتان."
        assert validation_message({}) == ""


@pytest.mark.parametrize(
    "translation",
    ["{max_length} حرف{expected_plural}", "{max_length:x} حرف"],
)
def test_translations_that_cannot_be_filled_fall_back(
    tmp_path: Path, translation: str
) -> None:
    """A field pydantic does not give, or a bad format, keeps pydantic's text."""
    write_catalog(
        tmp_path,
        "ar",
        'msgctxt "validation"\n'
        'msgid "String should have at most {max_length} character{expected_plural}"\n'
        f'msgstr "{translation}"\n',
    )
    error = validation_error(reference="abcdefg", quantity=1)
    with override_settings(LOCALE_PATHS=[tmp_path]), override("ar"):
        assert validation_message(error) == "String should have at most 5 characters"


def test_only_numbers_are_localized() -> None:
    """Integers, floats, and decimals use the language's digits; booleans do not."""
    with override("ar"):
        assert _localized(12) == "\u0661\u0662"
        assert _localized(1.5) == "\u0661\u066b\u0665"
        assert _localized(True) is True
        assert _localized("12") == "12"


# Settings ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"LANGUAGES": []}, "at least one language"),
        ({"LANGUAGES": [("en", "English"), ("pt-BR", "Portuguese")]}, "lower case"),
        ({"LANGUAGES": [("en", "English"), ("en", "Again")]}, "twice"),
        ({"LANGUAGE_CODE": "ar", "LANGUAGES": [("en", "English")]}, "not in LANGUAGES"),
        ({"LANGUAGE_CODE": "EN"}, "String should match pattern"),
        ({"TIME_ZONE": "Mars/Olympus"}, "not a time zone"),
        ({"TIME_ZONE": "../etc/passwd"}, "not a time zone"),
    ],
)
def test_language_and_zone_settings_are_checked(
    values: dict[str, object], message: str
) -> None:
    """Unusable languages and zones fail at startup."""
    with pytest.raises(ValidationError, match=message):
        Settings.model_validate(values)


def test_language_settings_defaults() -> None:
    """English and Arabic, English first; times shown in UTC."""
    defaults = Settings()
    assert defaults.LANGUAGE_CODE == "en"
    assert defaults.LANGUAGES == (("en", "English"), ("ar", "Arabic"))
    assert (defaults.LOCALE_PATHS, defaults.TIME_ZONE) == ((), "UTC")
    assert Settings.model_validate(
        {"LANGUAGE_CODE": "pt-br", "LANGUAGES": [("pt-br", "Português")]}
    )


# The middleware ----------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    """An application with ``LocaleMiddleware`` and the sample routes."""
    with override_settings(
        ROOT_ROUTER="domainkit_tests.sample_platform_routes.router",
        MIDDLEWARE=MIDDLEWARE,
    ):
        yield TestClient(get_asgi_application())


def test_errors_are_answered_in_the_callers_language(client: TestClient) -> None:
    """Details and validation messages in Arabic; codes and types unchanged."""
    arabic = {"Accept-Language": "ar-EG, en;q=0.5"}
    missing = client.get("/nowhere", headers=arabic)
    invalid = client.post("/orders", json={"reference": "abcdefg"}, headers=arabic)
    domain = client.get("/missing-user", headers=arabic)
    english = client.get("/nowhere")

    assert (missing.status_code, missing.json()["code"]) == (404, "not_found")
    assert missing.json()["detail"] == "غير موجود"
    assert missing.headers["Content-Language"] == "ar"
    assert missing.headers["Vary"] == "Accept-Language"
    body = invalid.json()
    assert body["detail"] == "الطلب غير صالح."
    assert [error["type"] for error in body["errors"]] == ["string_too_long", "missing"]
    assert [error["msg"] for error in body["errors"]] == [
        "يجب ألا يزيد عدد الأحرف على \u0665",
        "هذا الحقل مطلوب",
    ]
    assert domain.json()["detail"] == "لا يوجد مستخدم كهذا."
    assert english.json()["detail"] == "Not Found"
    assert english.headers["Content-Language"] == "en"


def test_the_request_is_served_in_its_language(client: TestClient) -> None:
    """Endpoints, and WebSocket connections, see the negotiated language."""
    served = client.get("/language", headers={"Accept-Language": "fr, ar;q=0.3"})
    with client.websocket_connect(
        "/language-ws", headers={"Accept-Language": "ar"}
    ) as socket:
        socket_language = socket.receive_text()

    assert served.json() == {"language": "ar"}
    assert socket_language == "ar"
    assert client.get("/language").json() == {"language": "en"}


def test_endpoints_may_label_responses_themselves(client: TestClient) -> None:
    """Their ``Content-Language`` stays; ``Vary`` is not repeated."""
    labelled = client.get("/labelled", headers={"Accept-Language": "ar"})

    assert labelled.headers["Content-Language"] == "en-gb"
    assert labelled.headers["Vary"] == "Origin, accept-language"


def test_one_language_varies_with_nothing() -> None:
    """With a single language, responses do not vary with ``Accept-Language``."""
    with override_settings(
        ROOT_ROUTER="domainkit_tests.sample_platform_routes.router",
        MIDDLEWARE=MIDDLEWARE,
        LANGUAGE_CODE="ar",
        LANGUAGES=[("ar", "Arabic")],
    ):
        response = TestClient(get_asgi_application()).get(
            "/nowhere", headers={"Accept-Language": "en"}
        )

    assert response.headers["Content-Language"] == "ar"
    assert "Vary" not in response.headers
    assert response.json()["detail"] == "غير موجود"


def test_other_scopes_and_headerless_starts_pass() -> None:
    """Lifespan events pass through; a start without headers gets them."""
    seen: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append({"type": scope["type"], "language": get_language()})
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 204})

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        seen.append(message)

    middleware = LocaleMiddleware(app)
    asyncio.run(middleware({"type": "lifespan"}, receive, send))
    asyncio.run(
        middleware(
            {"type": "http", "headers": [(b"accept-language", b"ar")]}, receive, send
        )
    )

    assert seen == [
        {"type": "lifespan", "language": "en"},
        {"type": "http", "language": "ar"},
        {
            "type": "http.response.start",
            "status": 204,
            "headers": [(b"content-language", b"ar"), (b"vary", b"Accept-Language")],
        },
    ]


@pytest.mark.parametrize(
    ("current", "names", "expected"),
    [
        (None, ["Accept-Language"], "Accept-Language"),
        ("Origin", ["Accept-Language", "origin"], "Origin, Accept-Language"),
        ("origin,  , ACCEPT-LANGUAGE", ["Accept-Language"], "origin, ACCEPT-LANGUAGE"),
        ("*", ["Accept-Language"], "*"),
        (None, ["A", "a", "B"], "A, B"),
        (None, [], None),
    ],
)
def test_vary_lists_each_header_once(
    current: str | None, names: list[str], expected: str | None
) -> None:
    """Names are added once, whatever their case; ``*`` stays alone."""
    headers = MutableHeaders(raw=[(b"vary", current.encode())] if current else [])

    patch_vary(headers, names)

    assert headers.get("Vary") == expected
