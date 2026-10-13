"""Errors raised by the framework itself (``django.core.exceptions``).

Business errors live in ``domainkit.ddd.exceptions``.
"""


class ImproperlyConfigured(Exception):
    """Settings, the environment, or the installed domains are invalid."""
