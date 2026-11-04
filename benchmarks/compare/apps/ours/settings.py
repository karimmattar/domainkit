"""The compared service on this framework: its defaults, with one domain."""

import os

ENVIRONMENT = "local"
DEBUG = False
PROJECT_NAME = "compare"
SECRET_KEY = os.environ["COMPARE_API_KEY"] * 3
ALLOWED_HOSTS = ["*"]
INSTALLED_DOMAINS = ["benchmarks.compare.apps.ours.catalog"]
ROOT_ROUTER = "benchmarks.compare.apps.ours.routes.router"
DATABASES = {"default": {"URL": os.environ["COMPARE_DATABASE_URL"], "POOL_SIZE": 10}}
API_DOCS = False
# No access log, as the other apps run under uvicorn --no-access-log.
ACCESS_LOG_SAMPLE_RATE = 0.0
