"""What reads untrusted input answers or refuses any input, and never crashes.

Each parser below takes what a client, a browser, or an authenticator
sends, and these tests send it anything: text of any characters, bytes of
any value, and the parser's own valid input broken at random. Every one
must return what it documents or raise the one error it documents; any
other exception is a 500 for a request. A request through a filter set
must be answered 200 or 422, whatever its query string.

CI runs them with the default example count; the weekly run, with
``--hypothesis-profile fuzz``, tries fifty times as many.
"""

import contextlib
from typing import Annotated, Any, Final, Literal

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from domainkit import signing
from domainkit.contrib.devices import webauthn
from domainkit.http.errors import install_exception_handlers
from domainkit.http.filters import FilterSet
from domainkit.i18n.translation import negotiate as negotiate_language
from domainkit.resources.renderers import NotAcceptable
from domainkit.resources.renderers import negotiate as negotiate_media
from domainkit.tracing import parse_traceparent, parse_tracestate
from domainkit.utils.headers import EntityTag, none_match, parse_tags
from domainkit_tests.gadgets import InMemoryGadgets
from domainkit_tests.test_filters import ITEMS, gadget_filters

SURROGATES: Final[tuple[Literal["Cs"]]] = ("Cs",)
"""Lone surrogates: no text a request carries holds one."""
KEY = "fuzz-key-" + "k" * 50
"""A signing key of its own, so no setting is read."""
HEADER_TEXT = st.text(
    alphabet=st.characters(codec="latin-1", exclude_categories=SURROGATES), max_size=200
)
"""What an HTTP header can hold: latin-1 text."""


def broken(valid: str) -> st.SearchStrategy[str]:
    """``valid`` with characters dropped, changed, or added at random places."""

    def edit(draw: st.DrawFn) -> str:
        text = valid
        for _ in range(draw(st.integers(min_value=1, max_value=4))):
            place = draw(st.integers(min_value=0, max_value=len(text)))
            kind = draw(st.sampled_from(["drop", "change", "add"]))
            letter = draw(st.characters(codec="utf-8", exclude_categories=SURROGATES))
            if kind == "drop":
                text = text[:place] + text[place + 1 :]
            elif kind == "change":
                text = text[:place] + letter + text[place + 1 :]
            else:
                text = text[:place] + letter + text[place:]
        return text

    return st.composite(edit)()


@given(HEADER_TEXT)
def test_entity_tags_parse_or_are_none(header: str) -> None:
    """``If-Match`` and ``If-None-Match``: a list of tags, or None."""
    tags = parse_tags(header)
    assert tags is None or all(isinstance(tag, EntityTag) for tag in tags)
    assert none_match(header, '"v1"') in {True, False}


@given(
    st.one_of(
        HEADER_TEXT, broken("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
    )
)
def test_trace_headers_parse_or_are_none(header: str) -> None:
    """``traceparent`` and ``tracestate``: their parts, or None."""
    parent = parse_traceparent(header)
    assert parent is None or all(isinstance(part, int) for part in parent)
    state = parse_tracestate(header)
    assert state is None or all(len(member) == 2 for member in state)


@given(st.one_of(st.text(max_size=300), broken(signing.dumps({"a": 1}, key=KEY))))
def test_signed_values_are_read_or_refused(value: str) -> None:
    """Only ``BadSignature``, unless the value is the one signed."""
    with contextlib.suppress(signing.BadSignature):
        assert signing.loads(value, key=KEY) == {"a": 1}


@given(st.binary(max_size=400))
def test_authenticator_data_parses_or_is_refused(data: bytes) -> None:
    """An authenticator's bytes: its fields, or ``WebAuthnError``."""
    try:
        parsed = webauthn.parse_authenticator_data(data)
    except webauthn.WebAuthnError:
        return
    assert isinstance(parsed, webauthn.AuthenticatorData)


@given(
    st.one_of(
        st.binary(max_size=300),
        st.builds(
            lambda head, rest: bytes([head]) + rest,
            st.integers(0, 255),
            st.binary(max_size=40),
        ),
    )
)
def test_cbor_decodes_or_is_refused(data: bytes) -> None:
    """A credential's key in CBOR: a value, or ``WebAuthnError``."""
    with contextlib.suppress(webauthn.WebAuthnError):
        webauthn.cbor_decode(data)


@given(st.one_of(st.binary(max_size=300), st.text(max_size=200).map(str.encode)))
def test_client_data_is_read_or_refused(data: bytes) -> None:
    """A browser's client data: its challenge, or ``WebAuthnError``."""
    with contextlib.suppress(webauthn.WebAuthnError):
        assert isinstance(webauthn.client_challenge(data), str)


@given(st.text(max_size=200))
def test_base64url_decodes_or_is_refused(value: str) -> None:
    """A credential id or a signature in base64url: bytes, or ``WebAuthnError``."""
    with contextlib.suppress(webauthn.WebAuthnError):
        assert isinstance(webauthn.b64decode(value), bytes)


OFFERED = ["application/json", "text/csv"]


@given(st.one_of(st.none(), HEADER_TEXT, broken("text/csv;q=0.9, application/*;q=0.1")))
def test_accept_picks_an_offered_type_or_refuses(header: str | None) -> None:
    """``Accept``: one of the types offered, or ``NotAcceptable`` (406)."""
    with contextlib.suppress(NotAcceptable):
        assert negotiate_media(header, OFFERED) in OFFERED


@given(st.one_of(st.none(), HEADER_TEXT, broken("ar-EG,ar;q=0.9,en;q=0.5")))
def test_accept_language_picks_a_language_offered(header: str | None) -> None:
    """``Accept-Language``: a language offered, or the default; never an error."""
    assert negotiate_language(header, ["en", "ar", "pt-br"], "en") in {
        "en",
        "ar",
        "pt-br",
    }


@pytest.fixture(scope="module")
def listing() -> TestClient:
    """A listing of gadgets through every kind of filter."""
    declared = gadget_filters()
    repository = InMemoryGadgets(ITEMS)
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/gadgets")
    async def gadgets(filters: Annotated[FilterSet, Depends(declared)]) -> list[str]:
        return [each.name for each in await repository.filter(filters.query())]

    return TestClient(app, raise_server_exceptions=False)


NAMES = st.sampled_from(
    [
        "name",
        "name__icontains",
        "name__istartswith",
        "price__gte",
        "price__lte",
        "stock",
        "stock__isnull",
        "stock__gt",
        "color",
        "color__in",
        "size__lt",
        "label__search",
        "made_at__gt",
        "active",
        "sku",
        "maker__iexact",
        "ordering",
        "search",
        "cursor",
        "limit",
    ]
)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.lists(
        st.tuples(
            st.one_of(NAMES, st.text(max_size=20)),
            st.one_of(
                st.text(max_size=60),
                st.sampled_from(["-name", "name,price", "1e999", "NaN", "-1", "red"]),
            ),
        ),
        max_size=6,
    )
)
def test_any_query_string_is_answered_or_refused(
    listing: TestClient, params: list[tuple[str, Any]]
) -> None:
    """200 with the names, or 422 with a code: never a 500."""
    response = listing.get("/gadgets", params=params)
    assert response.status_code in {200, 422}, (params, response.text)
    if response.status_code == 422:
        assert response.json()["code"]
