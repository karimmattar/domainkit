"""Content types: one row per kind of stored object (Django's ``contenttypes``).

Every class mapped onto a table of an installed domain (an aggregate, or any
other mapped entity) is a content type, named ``<domain label>.<model>``,
where the model is the class name in lower case: ``billing.invoice``.
``migrate`` creates the rows, and ``domainkit.contrib.auth`` attaches
permissions to them. A class describes itself in a nested ``Meta``::

    class Invoice(AggregateRoot[InvoiceId]):
        class Meta:
            verbose_name = "invoice"
            permissions = (("approve_invoice", "Can approve invoice"),)

Which classes are models, their keys, and their ``Meta`` options are the
core's (``domainkit.db.models``) since 0.10, so rules and resources that
name models work without this app. 0.12 listed them here too (with
``content_type_key`` for ``model_key``); those names still import from
here, with a ``DomainkitDeprecationWarning``, until domainkit 2.0.
"""

from domainkit.deprecation import renamed

from .models import ContentType
from .sync import sync_content_types

__all__ = [
    "ContentType",
    "sync_content_types",
]

__getattr__ = renamed(
    __name__,
    {
        "DEFAULT_PERMISSIONS": "domainkit.db.models.DEFAULT_PERMISSIONS",
        "InstalledModel": "domainkit.db.models.InstalledModel",
        "ModelOptions": "domainkit.db.models.ModelOptions",
        "content_type_key": "domainkit.db.models.model_key",
        "installed_models": "domainkit.db.models.installed_models",
        "model_options": "domainkit.db.models.model_options",
    },
    "2.0",
)
