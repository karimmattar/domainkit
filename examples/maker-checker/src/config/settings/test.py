"""Settings for the tests (domainkit_settings in pyproject.toml)."""

from config.settings.base import *
from config.settings.base import AUTH, env

ENVIRONMENT = "test"
ALLOWED_HOSTS = ["testserver"]
SECRET_KEY = "domainkit-insecure-test-key"
# In-memory SQLite, unless PAYOUTS_API_TEST_DATABASE_URL names PostgreSQL; then
# PAYOUTS_API_TEST_DATABASE_OWNER_URL may name the schema owner, and the tests
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
