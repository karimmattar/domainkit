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
name models work without this app; the names stay here as well, the same
objects (``content_type_key`` is ``model_key``).
"""

from domainkit.db.models import (
    DEFAULT_PERMISSIONS,
    InstalledModel,
    ModelOptions,
    installed_models,
    model_options,
)
from domainkit.db.models import model_key as content_type_key

from .models import ContentType
from .sync import sync_content_types

__all__ = [
    "DEFAULT_PERMISSIONS",
    "ContentType",
    "InstalledModel",
    "ModelOptions",
    "content_type_key",
    "installed_models",
    "model_options",
    "sync_content_types",
]
