"""Settings every rung starts from; each overrides what it measures.

``DOMAINKIT_BENCH_MIGRATIONS`` names the directory the database's
migrations are written to (``python -m benchmarks`` makes one per run).
"""

import os
import tempfile
from pathlib import Path

ENVIRONMENT = "local"
DEBUG = False
PROJECT_NAME = "bench"
BASE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = Path(
    os.environ.get("DOMAINKIT_BENCH_MIGRATIONS")
    or Path(tempfile.gettempdir()) / "domainkit-bench-migrations"
)
ALLOWED_HOSTS = ["api.example.com"]
# The domains the init template installs, idempotency, and the catalog.
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    "domainkit.contrib.idempotency",
    "benchmarks.project.catalog",
]
ROOT_ROUTER = "benchmarks.project.routes.router"
DATABASES = {"default": {"URL": "sqlite+aiosqlite:///:memory:"}}
API_DOCS = False
# Users are made once, when the database is: hashing cost is not measured.
AUTH = {"PASSWORD_HASHERS": ["domainkit.contrib.auth.hashers.FastPBKDF2PasswordHasher"]}
