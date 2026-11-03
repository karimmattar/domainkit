"""``GZipMiddleware``: responses compressed for clients that accept gzip."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from domainkit.conf import override_settings
from domainkit.http.compression import GZipMiddleware

BODY = "domainkit " * 100


def client() -> TestClient:
    """An application with a long and a short answer, behind the middleware."""
    app = FastAPI()

    @app.get("/long")
    def long() -> PlainTextResponse:
        return PlainTextResponse(BODY)

    @app.get("/short")
    def short() -> PlainTextResponse:
        return PlainTextResponse("hi")

    app.add_middleware(GZipMiddleware)
    return TestClient(app)


def test_long_answers_are_compressed_for_clients_that_accept_gzip() -> None:
    """Past ``GZIP_MINIMUM_SIZE``, at ``GZIP_LEVEL``; short ones as they are."""
    with override_settings(GZIP_MINIMUM_SIZE=100, GZIP_LEVEL=9):
        served = client()
        compressed = served.get("/long", headers={"Accept-Encoding": "gzip"})
        plain = served.get("/long", headers={"Accept-Encoding": "identity"})
        short = served.get("/short", headers={"Accept-Encoding": "gzip"})

    assert compressed.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in compressed.headers["Vary"]
    assert compressed.text == BODY  # httpx decodes it.
    assert int(compressed.headers["Content-Length"]) < len(BODY) // 5
    assert "Content-Encoding" not in plain.headers
    assert "Content-Encoding" not in short.headers
