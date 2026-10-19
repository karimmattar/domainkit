"""Minimal settings for framework tests, independent of the project's domains."""

ENVIRONMENT = "test"
ALLOWED_HOSTS = ["testserver"]
INSTALLED_DOMAINS = ["domainkit_tests.sample_domains.alpha"]
ROOT_ROUTER = "domainkit_tests.sample_routes.router"
DATABASES = {"default": {"URL": "sqlite+aiosqlite:///:memory:"}}
ALPHA = {"GREETING": "hi"}
# Probe every time; caching has its own test.
READINESS_CACHE_SECONDS = 0
# Random enough for the deployment checks, which some tests switch on.
SECRET_KEY = "tests-only-6Zq3b1Jw8RkV0yXc5MnHf2LpT9sDg4AeU7oKiWx3N"
