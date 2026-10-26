"""The documentation pages, their own copies of Swagger UI and ReDoc, and CSP."""

import hashlib
import re
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import domainkit
from domainkit.conf import UNSET, override_settings, unwrap_settings
from domainkit.conf.global_settings import (
    DEFAULT_CONTENT_SECURITY_POLICY,
    DEFAULT_PERMISSIONS_POLICY,
    Settings,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.apidocs import (
    ASSETS,
    REDOC_POLICY,
    REDOC_VERSION,
    STATIC,
    SWAGGER_POLICY,
    SWAGGER_UI_VERSION,
)
from domainkit.http.application import api_docs

DOCS: Final = ("/docs", "/redoc", "/openapi.json")
VENDORED: Final = {
    "swagger-ui/swagger-ui-bundle.js": (
        "050bc415ee7048dcd881682678f720264e7da5e373f7461d7c58c755305255f7"
    ),
    "swagger-ui/swagger-ui.css": (
        "1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321"
    ),
    "redoc/redoc.standalone.js": (
        "dcaf76612bc4a3fbcc923a8966dee2f6146a5f32e5ce1b6f02dd60cbbf89500b"
    ),
}
"""The npm releases' files, byte for byte (``swagger-ui-dist`` 5.33.1, ``redoc``
2.5.4): an upgrade changes these on purpose."""
EXTERNAL: Final = re.compile(r"""(?:src|href)=["']?(?:https?:)?//""")


def served(**overrides: object) -> dict[str, int]:
    """The status of each documentation path, with ``overrides``."""
    with (
        override_settings(**overrides),
        TestClient(get_asgi_application()) as client,
    ):
        return {path: client.get(path).status_code for path in DOCS}


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client of the test application, documentation on."""
    with ExitStack() as stack:
        stack.enter_context(override_settings(API_DOCS=True))
        yield stack.enter_context(TestClient(get_asgi_application()))


def test_a_default_deployment_serves_no_documentation() -> None:
    """Unset, ``API_DOCS`` follows ``DEBUG``: off in production, on locally."""
    off = dict.fromkeys(DOCS, 404)
    on = dict.fromkeys(DOCS, 200)

    assert served(ENVIRONMENT="production", API_DOCS=UNSET) == off
    assert served(API_DOCS=UNSET) == off  # DEBUG is False unless set.
    assert served(API_DOCS=UNSET, DEBUG=True) == on
    assert served(ENVIRONMENT="production", API_DOCS=True) == on
    assert served(API_DOCS=False, DEBUG=True) == off


@pytest.mark.parametrize(
    ("explicit", "debug", "expected"),
    [
        (None, False, False),
        (None, True, True),
        (True, False, True),
        (False, True, False),
    ],
)
def test_api_docs_follows_debug_unless_set(
    explicit: bool | None, debug: bool, expected: bool
) -> None:
    """The one place that decides it."""
    with override_settings(API_DOCS=explicit, DEBUG=debug):
        assert api_docs() is expected


def test_only_documentation_turned_on_is_warned_about_when_deployed() -> None:
    """Off by default in production: no warning; on there: the warning."""
    with override_settings(ENVIRONMENT="production", API_DOCS=UNSET):
        unset = unwrap_settings()
    with override_settings(ENVIRONMENT="production", API_DOCS=True):
        on = unwrap_settings()

    assert not any("API_DOCS" in each for each in unset.deployment_warnings())
    assert any("API_DOCS" in each for each in on.deployment_warnings())


@pytest.mark.parametrize(
    ("path", "policy", "assets"),
    [
        ("/docs", SWAGGER_POLICY, ["swagger-ui.css", "swagger-ui-bundle.js"]),
        ("/redoc", REDOC_POLICY, ["redoc.standalone.js"]),
    ],
)
def test_pages_load_nothing_from_elsewhere(
    client: TestClient, path: str, policy: str, assets: list[str]
) -> None:
    """Every script and style is the API's own, under a policy that says so."""
    page = client.get(path)

    assert page.headers["content-type"] == "text/html; charset=utf-8"
    assert page.headers["content-security-policy"] == policy
    assert "script-src 'self';" in policy
    assert "unsafe-eval" not in policy
    assert EXTERNAL.search(page.text) is None
    assert "<script>" not in page.text  # No inline script: none would run.
    for name in assets:
        assert f'="/docs/assets/{name}"' in page.text
    assert 'openapi.json"' in page.text


def test_swagger_ui_is_started_by_a_file(client: TestClient) -> None:
    """The start script reads the document's URL from the page."""
    script = client.get("/docs/assets/swagger-init.js")

    assert script.headers["content-type"].startswith("text/javascript")
    assert "element.dataset.openapiUrl" in script.text
    assert 'data-openapi-url="/openapi.json"' in client.get("/docs").text


def test_pages_name_the_document_under_the_root_path() -> None:
    """Behind ``--root-path /api``, as FastAPI's own pages do; titles escaped."""
    with (
        override_settings(API_DOCS=True, PROJECT_NAME="Shop <API>"),
        TestClient(get_asgi_application(), root_path="/api") as client,
    ):
        swagger = client.get("/api/docs").text
        redoc = client.get("/api/redoc").text

    assert 'data-openapi-url="/api/openapi.json"' in swagger
    assert 'href="/api/docs/assets/swagger-ui.css"' in swagger
    assert '<redoc spec-url="/api/openapi.json">' in redoc
    assert "<title>Shop &lt;API&gt; - ReDoc</title>" in redoc


@pytest.mark.parametrize("name", sorted(ASSETS))
def test_assets_are_served_with_their_type_and_kept_a_day(
    client: TestClient, name: str
) -> None:
    """JavaScript and CSS, from the package's files."""
    answer = client.get(f"/docs/assets/{name}")

    assert answer.status_code == 200
    assert answer.content == (STATIC / ASSETS[name]).read_bytes()
    kind = "text/css" if name.endswith(".css") else "text/javascript"
    assert answer.headers["content-type"].startswith(kind)
    assert answer.headers["cache-control"] == "public, max-age=86400"


@pytest.mark.parametrize(
    "name", ["LICENSE", "swagger-ui%2Fswagger-ui.css", "..%2Fapidocs.py", "x.js"]
)
def test_nothing_else_is_served(client: TestClient, name: str) -> None:
    """Only the listed files: no license, other file, or path."""
    assert client.get(f"/docs/assets/{name}").status_code == 404


def test_the_pages_are_not_in_the_document(client: TestClient) -> None:
    """As FastAPI's own pages, they are left out of OpenAPI."""
    paths = client.get("/openapi.json").json()["paths"]

    assert not [path for path in paths if path.startswith(("/docs", "/redoc"))]


def test_vendored_files_are_the_releases_own() -> None:
    """Byte for byte, with their licenses next to them."""
    assert (SWAGGER_UI_VERSION, REDOC_VERSION) == ("5.33.1", "2.5.4")
    for name, digest in VENDORED.items():
        assert hashlib.sha256((STATIC / name).read_bytes()).hexdigest() == digest
    for name in ("swagger-ui/LICENSE", "swagger-ui/NOTICE", "redoc/LICENSE"):
        assert (STATIC / name).is_file()


def test_every_response_has_the_security_baseline(client: TestClient) -> None:
    """CSP, Permissions-Policy, and COOP, unless the route set its own."""
    answer = client.get("/ok")

    assert answer.headers["content-security-policy"] == DEFAULT_CONTENT_SECURITY_POLICY
    assert answer.headers["permissions-policy"] == DEFAULT_PERMISSIONS_POLICY
    assert answer.headers["cross-origin-opener-policy"] == "same-origin"
    assert (
        DEFAULT_CONTENT_SECURITY_POLICY == "default-src 'none'; frame-ancestors 'none'"
    )


def test_the_baseline_headers_are_settings() -> None:
    """Each can be changed, or left out with None."""
    with (
        override_settings(
            SECURE_CONTENT_SECURITY_POLICY="default-src 'self'",
            SECURE_PERMISSIONS_POLICY=None,
            SECURE_CROSS_ORIGIN_OPENER_POLICY=None,
        ),
        TestClient(get_asgi_application()) as client,
    ):
        answer = client.get("/ok")

    assert answer.headers["content-security-policy"] == "default-src 'self'"
    assert "permissions-policy" not in answer.headers
    assert "cross-origin-opener-policy" not in answer.headers


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SECURE_CONTENT_SECURITY_POLICY", "default-src 'none'\r\nX-Evil: 1"),
        ("SECURE_CONTENT_SECURITY_POLICY", ""),
        ("SECURE_CONTENT_SECURITY_POLICY", "x" * 4097),
        ("SECURE_PERMISSIONS_POLICY", "camera=()\n"),
        ("SECURE_PERMISSIONS_POLICY", "caméra=()"),
        ("SECURE_CROSS_ORIGIN_OPENER_POLICY", "same-site"),
    ],
)
def test_baseline_header_settings_are_bounded(name: str, value: str) -> None:
    """Printable ASCII on one line, at most 4096 characters; COOP's own values."""
    with pytest.raises(ValidationError, match=name):
        Settings.model_validate({name: value})
    with (
        pytest.raises(ImproperlyConfigured, match=name),
        override_settings(**{name: value}),
    ):
        pass


def test_the_project_template_documents_the_switch() -> None:
    """``init``'s settings say where the docs are served and how to change it."""
    settings_dir = (
        Path(domainkit.__file__).parent
        / "management/templates/project/src/config/settings"
    )
    base = (settings_dir / "base.py-tpl").read_text(encoding="utf-8")
    production = (settings_dir / "production.py-tpl").read_text(encoding="utf-8")

    assert "/docs, /redoc, and /openapi.json follow DEBUG" in base
    assert "API_DOCS = True serves them anywhere" in base
    assert 'API_DOCS = env.bool("API_DOCS", False)' in production


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SECURE_REFERRER_POLICY", "no-referrer\r\nSet-Cookie: session=attacker"),
        ("SECURE_REFERRER_POLICY", "no-referer"),
        ("SECURE_REFERRER_POLICY", "origin,"),
        ("CORS_ALLOWED_ORIGINS", ["https://app.example\r\nX-Evil: 1"]),
        ("CORS_ALLOWED_ORIGINS", ["https://app.example/path"]),
        ("CORS_ALLOWED_METHODS", ["GET\r\nX-Evil: 1"]),
        ("CORS_ALLOWED_HEADERS", ["X-Trace, X-Evil"]),
    ],
)
def test_header_values_from_settings_refuse_line_breaks(
    name: str, value: object
) -> None:
    """SEC-10c-5: the referrer policy and CORS values are checked like the CSP."""
    with (
        pytest.raises(ImproperlyConfigured, match=name),
        override_settings(**{name: value}),
    ):
        pass


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SECURE_REFERRER_POLICY", "strict-origin-when-cross-origin"),
        ("SECURE_REFERRER_POLICY", "no-referrer, strict-origin-when-cross-origin"),
        ("CORS_ALLOWED_ORIGINS", ["https://app.example:8443", "*"]),
        ("CORS_ALLOWED_METHODS", ["*"]),
        ("CORS_ALLOWED_HEADERS", ["X-Request-ID", "*"]),
    ],
)
def test_valid_header_settings_still_load(name: str, value: object) -> None:
    """Every policy, a list of them, a port, and ``*`` stay valid."""
    with override_settings(**{name: value}):
        pass
