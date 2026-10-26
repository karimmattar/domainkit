"""The API's documentation pages, from domainkit's own copies (``API_DOCS``).

``/docs`` (Swagger UI) and ``/redoc`` (ReDoc) load their scripts and styles
from the API's own origin, under ``/docs/assets/``, never from a CDN: a
page that runs a CDN's script in the API's origin hands that CDN whatever
the origin holds (cookies, tokens in storage). Each page answers with a
policy of its own that allows scripts from the origin only, and no inline
script: Swagger UI is started by a file (``swagger-init.js``) that reads
the document's URL from the page. ``SWAGGER_POLICY`` allows nothing more
than Swagger UI uses; ``REDOC_POLICY`` adds the inline styles ReDoc sets
and the ``blob:`` worker it searches in. Every other response keeps
``SECURE_CONTENT_SECURITY_POLICY``.

The files in ``static/`` are the npm releases' own, unchanged:
``swagger-ui-dist`` 5.33.1 (``swagger-ui-bundle.js``, ``swagger-ui.css``;
Apache-2.0, ``LICENSE`` and ``NOTICE``) and ``redoc`` 2.5.4
(``bundles/redoc.standalone.js``; MIT). To upgrade, replace them with a
newer release's, and the versions and hashes the tests pin.
"""

import html
from pathlib import Path
from typing import Final

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.routing import Route

__all__ = [
    "REDOC_POLICY",
    "SWAGGER_POLICY",
    "docs_routes",
]

STATIC: Final = Path(__file__).parent / "static"
SWAGGER_UI_VERSION: Final = "5.33.1"
REDOC_VERSION: Final = "2.5.4"
ASSETS: Final = {
    "swagger-ui-bundle.js": "swagger-ui/swagger-ui-bundle.js",
    "swagger-ui.css": "swagger-ui/swagger-ui.css",
    "swagger-init.js": "swagger-ui/swagger-init.js",
    "redoc.standalone.js": "redoc/redoc.standalone.js",
}
"""What ``/docs/assets/<name>`` serves: these files, and no other."""
MEDIA_TYPES: Final = {".js": "text/javascript", ".css": "text/css"}
ASSET_CACHE: Final = "public, max-age=86400"
"""Browsers keep the assets for a day; they change with domainkit's version."""
SWAGGER_POLICY: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)
"""``/docs``'s Content-Security-Policy: scripts and styles from the origin only;
its icons are ``data:`` images, and "Try it out" calls the API itself."""
REDOC_POLICY: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; worker-src blob:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
"""``/redoc``'s: as ``/docs``'s, but ReDoc sets its styles inline, and searches
in a worker it makes from a ``blob:``. Its footer's logo, from Redocly's CDN,
is refused."""
DOCS_PATH: Final = "/docs"
REDOC_PATH: Final = "/redoc"
OPENAPI_PATH: Final = "/openapi.json"

SWAGGER_PAGE: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - Swagger UI</title>
<link rel="stylesheet" href="{root}/docs/assets/swagger-ui.css">
</head>
<body>
<div id="swagger-ui" data-openapi-url="{openapi}"></div>
<script src="{root}/docs/assets/swagger-ui-bundle.js"></script>
<script src="{root}/docs/assets/swagger-init.js"></script>
</body>
</html>
"""
REDOC_PAGE: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - ReDoc</title>
</head>
<body>
<redoc spec-url="{openapi}"></redoc>
<script src="{root}/docs/assets/redoc.standalone.js"></script>
</body>
</html>
"""


def docs_routes(title: str) -> list[Route]:
    """``/docs``, ``/redoc``, and their assets, for an API called ``title``.

    Plain Starlette routes, out of the OpenAPI document, as FastAPI's own
    documentation routes are. The pages name ``/openapi.json`` under the
    request's root path, as FastAPI's do.
    """
    name = html.escape(title)

    def page(template: str, policy: str, request: Request) -> HTMLResponse:
        root = html.escape(request.scope.get("root_path", "").rstrip("/"))
        return HTMLResponse(
            template.format(title=name, root=root, openapi=f"{root}{OPENAPI_PATH}"),
            headers={"Content-Security-Policy": policy},
        )

    async def swagger_ui(request: Request) -> Response:
        """Swagger UI, from the API's own copy."""
        return page(SWAGGER_PAGE, SWAGGER_POLICY, request)

    async def redoc(request: Request) -> Response:
        """ReDoc, from the API's own copy."""
        return page(REDOC_PAGE, REDOC_POLICY, request)

    async def asset(request: Request) -> Response:
        """One of the files ``ASSETS`` lists; any other name is 404."""
        file = ASSETS.get(request.path_params["name"])
        if file is None:
            raise HTTPException(status_code=404)
        path = STATIC / file
        return FileResponse(
            path,
            media_type=MEDIA_TYPES[path.suffix],
            headers={"Cache-Control": ASSET_CACHE},
        )

    return [
        Route(DOCS_PATH, swagger_ui, include_in_schema=False),
        Route(REDOC_PATH, redoc, include_in_schema=False),
        Route(f"{DOCS_PATH}/assets/{{name}}", asset, include_in_schema=False),
    ]
