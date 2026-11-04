"""The compared service on Django: async views and the ORM, as its docs teach it."""

import os
from urllib.parse import urlsplit

url = urlsplit(os.environ["COMPARE_DATABASE_URL"].replace("+asyncpg", ""))
SECRET_KEY = os.environ["COMPARE_API_KEY"] * 3
DEBUG = False
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "benchmarks.compare.apps.django_app.urls"
INSTALLED_APPS = ["benchmarks.compare.apps.django_app"]
MIDDLEWARE: list[str] = []
USE_TZ = True
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": url.path.lstrip("/") or "postgres",
        "USER": url.username or "postgres",
        "PASSWORD": url.password or "",
        "HOST": url.hostname
        or dict(p.split("=") for p in url.query.split("&") if p).get("host", ""),
        "PORT": url.port
        or dict(p.split("=") for p in url.query.split("&") if p).get("port", ""),
        "OPTIONS": {"pool": {"min_size": 1, "max_size": 10}},
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
