"""0.12's home of the database's clock, which is ``domainkit.db.dbclock`` now.

Its names still import from here, with a ``DomainkitDeprecationWarning``,
until domainkit 2.0: import them from ``domainkit.db.dbclock``.
"""

from domainkit.deprecation import moved

__getattr__ = moved(
    __name__,
    dict.fromkeys(("Moment", "at", "now", "uses_database"), "domainkit.db.dbclock"),
    "2.0",
)
