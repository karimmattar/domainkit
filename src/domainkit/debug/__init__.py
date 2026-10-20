"""Development aids: SQL per request, likely N+1 queries, and profiles.

List ``DebugToolbarMiddleware`` in a development settings module's
``MIDDLEWARE``, right after ``RequestContextMiddleware``. It works only
while ``DEBUG`` is on, and staging and production refuse to start with it.
"""

from domainkit.debug.middleware import DebugToolbarMiddleware
from domainkit.debug.queries import QueryLog, record_queries, statement_shape

__all__ = ["DebugToolbarMiddleware", "QueryLog", "record_queries", "statement_shape"]
