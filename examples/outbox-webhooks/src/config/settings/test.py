"""Settings for the tests (domainkit_settings in pyproject.toml)."""

from config.settings.base import *
from config.settings.base import AUTH, env

ENVIRONMENT = "test"
ALLOWED_HOSTS = ["testserver"]
SECRET_KEY = "domainkit-insecure-test-key"
# In-memory SQLite, unless TRANSFERS_API_TEST_DATABASE_URL names PostgreSQL; then
# TRANSFERS_API_TEST_DATABASE_OWNER_URL may name the schema owner, and the tests
# run as the application's own role (domainkit's docs/testing.md, "Test
# databases"). domainkit's CI runs this example both ways.
DATABASES = {
    "default": {
        "URL": env.str("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:"),
        "MIGRATION_URL": env.str("TEST_DATABASE_OWNER_URL", None),
    }
}
# One hash iteration: fast tests. Staging and production refuse it.
AUTH = {
    **AUTH,  # keeps a user model of your own (USER_MODEL, USER_TABLE)
    "PASSWORD_HASHERS": ["domainkit.contrib.auth.hashers.FastPBKDF2PasswordHasher"],
}

# The tests deliver webhooks to a receiver on 127.0.0.1 (tests/conftest.py),
# a network deployments refuse here, and give up on a message after three
# attempts rather than ten.
WEBHOOKS = {"ALLOWED_PRIVATE_NETWORKS": ["127.0.0.0/8"], "MAX_ATTEMPTS": 3}
MESSAGING = {"MAX_ATTEMPTS": 3}
