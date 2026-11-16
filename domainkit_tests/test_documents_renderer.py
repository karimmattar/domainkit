"""The renderer port, its Gotenberg adapter, and ``DOCUMENTS``' settings and checks.

Gotenberg is faked with the HTTP client's ``stub``: nothing reaches the
network. The fake reads the multipart form as Gotenberg would.
"""

import io
import sys
from collections.abc import Iterator
from email import policy
from email.parser import BytesParser
from typing import Any, Final, cast

import httpx2
import pytest
from pydantic import ValidationError

from domainkit.conf import Settings, override_settings
from domainkit.conf.global_settings import RendererConfig, is_loopback
from domainkit.context import RequestContext, bind_request
from domainkit.documents import (
    PageOptions,
    Renderer,
    RendererUnavailable,
    RenderingRefused,
    renderer,
)
from domainkit.documents.gotenberg import (
    CHROMIUM_HTML,
    LIBREOFFICE,
    MAX_ASSETS,
    GotenbergRenderer,
    encode_form,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.client import ResponseTooLarge, stub
from domainkit.http.errors import upstream_refusal
from domainkit.i18n import gettext, override
from domainkit.management import call_command
from domainkit.management.base import CommandError

pytestmark = pytest.mark.anyio

URL: Final = "https://gotenberg.internal:3000"
PDF: Final = b"%PDF-1.7\n%fake\n"
SECRET_KEY: Final = "tests-only-6Zq3b1Jw8RkV0yXc5MnHf2LpT9sDg4AeU7oKiWx3N"
DEPLOYED: Final = {
    "ENVIRONMENT": "production",
    "ALLOWED_HOSTS": ["api.example.com"],
    "SECRET_KEY": SECRET_KEY,
}
LOGIN: Final = {"USERNAME": "shop", "PASSWORD": "s3cret-pass"}
ARABIC: Final = "<p dir='rtl' lang='ar'>عقد إيجار بين الطرفين</p>"


class Form:
    """A multipart request as Gotenberg reads it: its fields and its files."""

    def __init__(self, request: httpx2.Request) -> None:
        """Read the form of ``request``."""
        head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
        message = BytesParser(policy=policy.HTTP).parsebytes(head + request.content)
        self.fields: dict[str, str] = {}
        self.files: dict[str, tuple[bytes, str]] = {}
        for part in message.iter_parts():
            payload = cast(bytes, part.get_payload(decode=True))
            filename = part.get_filename()
            if filename is None:
                name = cast(str, part.get_param("name", header="content-disposition"))
                self.fields[name] = payload.decode()
            else:
                assert part.get_param("name", header="content-disposition") == "files"
                self.files[filename] = (payload, part.get_content_type())


class Gotenberg:
    """A fake Gotenberg: records each request, answers what it is told."""

    def __init__(self, *answers: httpx2.Response) -> None:
        """Answer ``answers`` in turn, then a PDF."""
        self.answers = list(answers)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Keep the request; give the next answer."""
        self.requests.append(request)
        if self.answers:
            return self.answers.pop(0)
        return httpx2.Response(200, content=PDF, headers={"Gotenberg-Trace": "t-1"})

    @property
    def form(self) -> Form:
        """The last request's form."""
        return Form(self.requests[-1])


@pytest.fixture
def no_retries() -> Iterator[None]:
    """A failing call is not tried again: no backoff to wait for."""
    with override_settings(HTTP_CLIENT={"RETRIES": 0}):
        yield


def run(*args: str) -> str:
    """Run a command; what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output)
    return output.getvalue()


# The settings -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("renderer_config", "message"),
    [
        ({"URL": "ftp://gotenberg"}, "http or https URL with a host"),
        ({"URL": "https://"}, "http or https URL with a host"),
        ({"URL": "gotenberg:3000"}, "http or https URL with a host"),
        ({"URL": "https://gotenberg:99999"}, "Port out of range"),
        ({"URL": "https://shop:pw@gotenberg"}, "not in the URL"),
        ({"URL": "https://gotenberg/?debug=1"}, "no query or fragment"),
        ({"URL": URL, "USERNAME": "shop"}, "USERNAME and PASSWORD together"),
        ({"URL": URL, "PASSWORD": "pw"}, "USERNAME and PASSWORD together"),
        ({"URL": URL, "USERNAME": "a:b", "PASSWORD": "pw"}, "USERNAME"),
        ({"URL": URL, "TIMEOUT": 3601}, "TIMEOUT"),
        ({"URL": URL, "TRACE_HEADER": "X Trace"}, "TRACE_HEADER"),
        ({"URL": URL, "SURPRISE": 1}, "SURPRISE"),
    ],
)
def test_a_renderer_is_an_http_url_and_a_login_or_none(
    renderer_config: dict[str, Any], message: str
) -> None:
    """A URL with a host and no login in it; a user and a password together."""
    with pytest.raises(ValidationError, match=message):
        Settings.model_validate({"DOCUMENTS": {"RENDERER": renderer_config}})


def test_the_renderer_settings_default_to_none() -> None:
    """No renderer unless set; Gotenberg's defaults once one is."""
    assert Settings().DOCUMENTS.RENDERER is None
    config = RendererConfig(URL=URL)
    assert (config.TIMEOUT, config.TRACE_HEADER) == (30.0, "Gotenberg-Trace")
    assert (config.USERNAME, config.PASSWORD, config.ALLOW_HTTP) == (None, None, False)
    assert "s3cret" not in repr(RendererConfig(URL=URL, **LOGIN))


@pytest.mark.parametrize(
    ("url", "allow_http", "refused"),
    [
        ("http://gotenberg:3000", False, True),
        ("http://10.0.0.7:3000", False, True),
        ("http://gotenberg:3000", True, False),
        ("https://gotenberg:3000", False, False),
        ("http://localhost:3000", False, False),
        ("http://LOCALHOST:3000", False, False),
        ("http://127.0.0.2:3000", False, False),
        ("http://[::1]:3000", False, False),
    ],
)
def test_staging_and_production_refuse_a_renderer_without_tls(
    url: str, allow_http: bool, refused: bool
) -> None:
    """Plain http to another host carries documents and the password in clear.

    A renderer on this host, or a network that encrypts by itself
    (``ALLOW_HTTP``), may keep http.
    """
    documents = {"RENDERER": {"URL": url, "ALLOW_HTTP": allow_http, **LOGIN}}
    local = Settings.model_validate(
        {**DEPLOYED, "ENVIRONMENT": "local", "DOCUMENTS": documents}
    )
    errors = local.deployment_errors()

    assert [error.id for error in errors] == (["documents.E001"] if refused else [])
    if refused:
        assert errors[0].message.startswith(
            "DOCUMENTS.RENDERER.URL must use https in local"
        )
        assert "ALLOW_HTTP = True" in errors[0].hint
        with pytest.raises(ValidationError, match=r"documents\.E001"):
            Settings.model_validate({**DEPLOYED, "DOCUMENTS": documents})
    else:
        Settings.model_validate({**DEPLOYED, "DOCUMENTS": documents})


def test_loopback_is_localhost_or_a_loopback_address() -> None:
    """Names other than localhost may resolve anywhere: they are not loopback."""
    assert [
        is_loopback(host)
        for host in ("localhost", "127.0.0.1", "::1", "gotenberg", "10.0.0.1")
    ] == [True, True, True, False, False]


def test_a_deployed_renderer_without_a_login_is_warned_of() -> None:
    """Gotenberg without basic auth renders, and fetches, for anyone who reaches it."""
    bare = Settings.model_validate(
        {**DEPLOYED, "DOCUMENTS": {"RENDERER": {"URL": URL}}}
    )
    logged_in = Settings.model_validate(
        {**DEPLOYED, "DOCUMENTS": {"RENDERER": {"URL": URL, **LOGIN}}}
    )
    local = Settings.model_validate({"DOCUMENTS": {"RENDERER": {"URL": URL}}})

    found = [w for w in bare.deployment_warnings() if w.startswith("DOCUMENTS")]
    assert [getattr(w, "id", None) for w in found] == ["documents.W001"]
    assert "--api-enable-basic-auth" in getattr(found[0], "hint", "")
    assert not [w for w in logged_in.deployment_warnings() if w.startswith("DOCUMENTS")]
    assert local.deployment_warnings() == []


def test_check_says_that_gotenbergs_flags_are_not_seen() -> None:
    """The flags are set where Gotenberg runs: check names them, as information."""
    production = {
        "ENVIRONMENT": "production",
        "CACHES": {"default": {"COMPUTE_TIMEOUT": 30}},
        "SILENCED_CHECKS": [
            "security.W001",
            "security.W002",
            "security.W003",
            "deploy.W005",
        ],
    }
    with override_settings(**production):
        without = run("check", "--deploy")
    with override_settings(**production, DOCUMENTS={"RENDERER": {"URL": URL, **LOGIN}}):
        checked = run("check", "--deploy")

    assert "documents.I001" not in without
    lines = checked.splitlines()
    info = lines.index(
        "  Info: documents.I001: Gotenberg's flags are not checked: they are set "
        "where it runs, and its API does not show them."
    )
    assert lines[info - 2].startswith("  Info: security.I001: HTTPS is not checked")
    assert lines[info + 1].startswith("    Hint: Start it with --chromium-disable-")
    assert lines[info + 1].endswith("(docs/documents.md, 'Hardening Gotenberg').")


def test_check_deploy_fails_on_a_renderer_without_a_login() -> None:
    """The warning gates a deployment pipeline until it is fixed or silenced."""
    production = {
        "ENVIRONMENT": "production",
        "CACHES": {"default": {"COMPUTE_TIMEOUT": 30}},
        "SILENCED_CHECKS": [
            "security.W001",
            "security.W002",
            "security.W003",
            "deploy.W005",
        ],
        "DOCUMENTS": {"RENDERER": {"URL": URL}},
    }
    with override_settings(**production), pytest.raises(CommandError) as refused:
        run("check", "--deploy")

    assert "documents.W001: DOCUMENTS.RENDERER has no USERNAME" in str(refused.value)


# The configured renderer --------------------------------------------------------------


def test_no_renderer_is_made_without_settings() -> None:
    """``renderer()`` names the setting to set."""
    with pytest.raises(ImproperlyConfigured, match=r'DOCUMENTS = \{"RENDERER"'):
        renderer()


def test_the_renderer_names_the_extra_it_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without httpx2, ``renderer()`` says which extra calls Gotenberg."""
    monkeypatch.setitem(sys.modules, "httpx2", None)
    monkeypatch.delitem(sys.modules, "domainkit.documents.gotenberg")

    with (
        override_settings(DOCUMENTS={"RENDERER": {"URL": "https://elsewhere:3000"}}),
        pytest.raises(ImportError) as missing,
    ):
        renderer()

    assert str(missing.value) == (
        'The renderer calls Gotenberg with httpx2: pip install "domainkit[httpclient]".'
    )


def test_the_renderer_is_made_once_per_configuration() -> None:
    """One client, so one circuit, while the settings hold; a new one after."""
    with override_settings(DOCUMENTS={"RENDERER": {"URL": URL, **LOGIN}}):
        made = renderer()
        again = renderer()
    with override_settings(DOCUMENTS={"RENDERER": {"URL": URL, "TIMEOUT": 5}}):
        other = renderer()

    assert made is again
    assert isinstance(made, GotenbergRenderer)
    assert isinstance(made, Renderer)
    assert other is not made
    assert isinstance(other, GotenbergRenderer)
    assert other.client.timeout == 5


# Gotenberg ----------------------------------------------------------------------------


async def test_html_is_printed_by_chromium_with_its_assets_and_the_login() -> None:
    """``index.html`` and the assets as files, the page as fields, basic auth."""
    fake = Gotenberg()
    with override_settings(DOCUMENTS={"RENDERER": {"URL": URL, **LOGIN}}):
        made = renderer()
    with stub(fake):
        pdf = await made.html_to_pdf(
            ARABIC,
            assets={"logo.png": b"\x89PNG", "NotoNaskh.ttf": b"font"},
            page=PageOptions(width="210mm", height="297mm", margin_top="1cm"),
        )

    assert pdf == PDF
    [request] = fake.requests
    assert (request.method, str(request.url)) == ("POST", URL + CHROMIUM_HTML)
    assert request.headers["authorization"] == "Basic c2hvcDpzM2NyZXQtcGFzcw=="
    assert "gotenberg-trace" not in request.headers  # No request to name.
    form = fake.form
    assert form.files == {
        "index.html": (ARABIC.encode(), "text/html"),
        "logo.png": (b"\x89PNG", "image/png"),
        "NotoNaskh.ttf": (b"font", "font/ttf"),
    }
    assert form.fields == {
        "landscape": "false",
        "scale": "1",
        "printBackground": "true",
        "preferCssPageSize": "true",
        "failOnResourceLoadingFailed": "true",
        "paperWidth": "210mm",
        "paperHeight": "297mm",
        "marginTop": "1cm",
    }


async def test_the_request_id_goes_to_gotenberg_and_its_trace_comes_back() -> None:
    """Gotenberg logs the call under the request's id; errors name its trace."""
    refusal = httpx2.Response(
        409,
        text="Chromium returned net::ERR_NAME_NOT_RESOLVED\nfor https://tracker",
        headers={"Gotenberg-Trace": "req-42"},
    )
    fake = Gotenberg(refusal)
    context = RequestContext(
        request_id="req-42", client_ip=None, method="POST", path="/contracts"
    )
    with stub(fake), bind_request(context), pytest.raises(RenderingRefused) as refused:
        await GotenbergRenderer(URL).html_to_pdf("<img src='https://tracker'>")

    assert fake.requests[0].headers["gotenberg-trace"] == "req-42"
    assert "authorization" not in fake.requests[0].headers
    assert (refused.value.status, refused.value.trace) == (409, "req-42")
    assert str(refused.value) == (
        "Gotenberg refused the document (409, trace 'req-42'): Chromium returned "
        "net::ERR_NAME_NOT_RESOLVED for https://tracker"
    )


async def test_a_renamed_trace_header_is_the_one_sent() -> None:
    """``TRACE_HEADER`` follows ``--api-correlation-id-header``."""
    fake = Gotenberg()
    context = RequestContext(request_id="r-7", client_ip=None, method="GET", path="/")
    with override_settings(
        DOCUMENTS={"RENDERER": {"URL": URL, "TRACE_HEADER": "X-Correlation-Id"}}
    ):
        made = renderer()
    with stub(fake), bind_request(context):
        await made.html_to_pdf("<p>x</p>")

    assert fake.requests[0].headers["x-correlation-id"] == "r-7"
    assert "gotenberg-trace" not in fake.requests[0].headers


async def test_an_office_document_is_converted_by_libreoffice() -> None:
    """One file, named so LibreOffice knows its kind; no field."""
    fake = Gotenberg()
    with stub(fake):
        pdf = await GotenbergRenderer(URL + "/gotenberg").office_to_pdf(
            b"PK\x03\x04", filename="Contract.DOCX"
        )

    assert pdf == PDF
    assert fake.requests[0].url == URL + "/gotenberg" + LIBREOFFICE
    assert fake.form.files == {
        "Contract.DOCX": (b"PK\x03\x04", "application/octet-stream")
    }
    assert fake.form.fields == {}


@pytest.mark.parametrize(
    "filename",
    ["contract.docm", "contract", "../contract.docx", "a b.docx", ".docx", "x.pdf"],
)
async def test_an_office_document_needs_a_plain_name_of_a_known_kind(
    filename: str,
) -> None:
    """No folder, no macros, nothing LibreOffice would guess at."""
    with pytest.raises(ValueError, match="plain file name ending in one of"):
        await GotenbergRenderer(URL).office_to_pdf(b"", filename=filename)


@pytest.mark.parametrize("name", ["index.html", "INDEX.HTML", "img/logo.png", "", "-x"])
async def test_an_asset_is_a_plain_name_other_than_index(name: str) -> None:
    """Gotenberg drops folders, and ``index.html`` is the page itself."""
    with pytest.raises(ValueError, match=r"plain file name other than index\.html"):
        await GotenbergRenderer(URL).html_to_pdf("<p/>", assets={name: b""})


async def test_a_page_links_a_bounded_number_of_assets() -> None:
    """At most ``MAX_ASSETS`` files go with one page."""
    assets = {f"a{n}.png": b"" for n in range(MAX_ASSETS + 1)}
    with pytest.raises(ValueError, match=f"{MAX_ASSETS} files at most"):
        await GotenbergRenderer(URL).html_to_pdf("<p/>", assets=assets)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"width": "21 cm"}, "width is a number and a unit"),
        ({"margin_left": "-1in"}, "margin_left is a number and a unit"),
        ({"height": "1e3"}, "height is a number and a unit"),
        ({"scale": 0.05}, "scale is 0.1 to 2"),
        ({"scale": 2.5}, "scale is 0.1 to 2"),
    ],
)
def test_page_options_are_lengths_and_a_scale_gotenberg_takes(
    options: dict[str, Any], message: str
) -> None:
    """Refused here, not by a 400 from the renderer."""
    with pytest.raises(ValueError, match=message):
        PageOptions(**options)


async def test_page_options_reach_the_form() -> None:
    """Every option, as Gotenberg spells it."""
    fake = Gotenberg()
    page = PageOptions(
        width="8.5in",
        height="11",
        margin_top="0",
        margin_bottom=".5in",
        margin_left="72pt",
        margin_right="96px",
        landscape=True,
        scale=0.75,
        print_background=False,
        prefer_css_page_size=False,
    )
    with stub(fake):
        await GotenbergRenderer(URL).html_to_pdf("<p/>", page=page)

    assert fake.form.fields == {
        "landscape": "true",
        "scale": "0.75",
        "printBackground": "false",
        "preferCssPageSize": "false",
        "failOnResourceLoadingFailed": "true",
        "paperWidth": "8.5in",
        "paperHeight": "11",
        "marginTop": "0",
        "marginBottom": ".5in",
        "marginLeft": "72pt",
        "marginRight": "96px",
    }


@pytest.mark.parametrize("status", [400, 413])
async def test_a_refusal_is_not_retried_and_says_why(status: int) -> None:
    """Gotenberg's reason, on one line and bounded, for the logs."""
    fake = Gotenberg(httpx2.Response(status, text="x" * 400 + "\n\nmore"))
    with stub(fake), pytest.raises(RenderingRefused) as refused:
        await GotenbergRenderer(URL).office_to_pdf(b"not a document")

    assert len(fake.requests) == 1
    assert refused.value.status == status
    assert refused.value.trace == ""
    assert str(refused.value).endswith(": " + "x" * 300)
    assert isinstance(refused.value, ValueError)


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.usefixtures("no_retries")
async def test_a_refused_login_is_an_unavailable_renderer(status: int) -> None:
    """The caller is answered 503; the logs name the settings to check."""
    fake = Gotenberg(httpx2.Response(status, headers={"Gotenberg-Trace": "t-9"}))
    with stub(fake), pytest.raises(RendererUnavailable) as unavailable:
        await GotenbergRenderer(URL, username="shop", password="wrong").html_to_pdf("")

    assert str(unavailable.value) == (
        f"Gotenberg refused the login ({status}): check "
        "DOCUMENTS[\"RENDERER\"]'s USERNAME and PASSWORD (trace 't-9')."
    )


@pytest.mark.usefixtures("no_retries")
async def test_a_failing_renderer_is_unavailable() -> None:
    """A 5xx once the retries are spent; something that is not a PDF."""
    with (
        stub(Gotenberg(httpx2.Response(503))),
        pytest.raises(RendererUnavailable) as failed,
    ):
        await GotenbergRenderer(URL).html_to_pdf("<p/>")
    with (
        stub(Gotenberg(httpx2.Response(200, content=b"PK\x03\x04 a zip"))),
        pytest.raises(RendererUnavailable) as other,
    ):
        await GotenbergRenderer(URL).html_to_pdf("<p/>")

    assert str(failed.value) == "Gotenberg answered 503 (trace '')."
    assert str(other.value) == (
        "Gotenberg answered with something that is not a PDF (trace '')."
    )
    refusal = upstream_refusal(failed.value)
    assert (refusal.status_code, refusal.code) == (503, "renderer_unavailable")
    assert refusal.detail == "Documents cannot be rendered now; try again later."
    with override("ar"):
        assert gettext(refusal.detail) == (
            "لا يمكن إنشاء المستندات الآن؛ أعد المحاولة لاحقًا."
        )


async def test_a_renderer_out_of_reach_is_unavailable() -> None:
    """A network error, once retried, becomes the renderer's own 503."""
    calls = []

    def refuse(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        raise httpx2.ConnectError("connection refused", request=request)

    with (
        override_settings(HTTP_CLIENT={"RETRIES": 1, "BACKOFF": 0}),
        stub(refuse),
        pytest.raises(
            RendererUnavailable, match=r"could not be reached \(ConnectError\)"
        ),
    ):
        await GotenbergRenderer(URL).html_to_pdf("<p/>")

    assert len(calls) == 2


async def test_a_rendering_is_retried_after_a_busy_answer() -> None:
    """Rendering changes nothing, so its POST is sent again after a 503."""
    fake = Gotenberg(httpx2.Response(503))
    with override_settings(HTTP_CLIENT={"RETRIES": 1, "BACKOFF": 0}), stub(fake):
        pdf = await GotenbergRenderer(URL).html_to_pdf("<p/>")

    assert pdf == PDF
    assert len(fake.requests) == 2


async def test_the_clients_own_upstream_errors_go_through() -> None:
    """A PDF past ``MAX_RESPONSE_BYTES`` is the client's 502, as any answer's."""
    big = httpx2.Response(200, content=PDF + b"x" * 64)
    with (
        override_settings(HTTP_CLIENT={"MAX_RESPONSE_BYTES": 32}),
        stub(Gotenberg(big)),
        pytest.raises(ResponseTooLarge),
    ):
        await GotenbergRenderer(URL).html_to_pdf("<p/>")


def test_a_login_is_a_user_and_a_password() -> None:
    """Both or neither; a basic auth user has no colon."""
    with pytest.raises(ValueError, match="together"):
        GotenbergRenderer(URL, username="shop")
    with pytest.raises(ValueError, match="together"):
        GotenbergRenderer(URL, password="pw")
    with pytest.raises(ValueError, match="no colon"):
        GotenbergRenderer(URL, username="a:b", password="pw")


def test_the_boundary_is_drawn_again_when_a_file_holds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file that holds the boundary would end its part early."""
    drawn = iter(["domainkit-aaaa", "domainkit-bbbb"])
    monkeypatch.setattr("domainkit.documents.gotenberg._boundary", lambda: next(drawn))

    body, kind = encode_form(
        {"x": "1"}, [("a.txt", b"--domainkit-aaaa--", "text/plain")]
    )

    assert kind == "multipart/form-data; boundary=domainkit-bbbb"
    assert body.startswith(
        b'--domainkit-bbbb\r\nContent-Disposition: form-data; name="x"'
    )
    assert body.endswith(b"--domainkit-bbbb--\r\n")
