"""The Django service's ASGI application."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE", "benchmarks.compare.apps.django_app.settings"
)
app = get_asgi_application()
