"""Settings of the gateway project: no ``domainkit.contrib`` app, no tables."""

ENVIRONMENT = "test"
ALLOWED_HOSTS = ["testserver"]
INSTALLED_DOMAINS: list[str] = []
ROOT_ROUTER = "domainkit_tests.gateway_project.router"
MIDDLEWARE = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit_tests.gateway_project.GatewayAuthentication",
]
DATABASES = {"default": {"URL": "sqlite+aiosqlite:///:memory:"}}
