"""Permissions on single objects (django-guardian's grants), for users or groups.

Install it after ``domainkit.contrib.auth``, add its backend after
``ModelBackend``, then ``makemigrations`` and ``migrate``::

    INSTALLED_DOMAINS = [
        ...,
        "domainkit.contrib.auth",
        "domainkit.contrib.objectpermissions",
    ]
    AUTH = {"BACKENDS": [
        "domainkit.contrib.auth.backends.ModelBackend",
        "domainkit.contrib.objectpermissions.ObjectPermissionBackend",
    ]}

Grant and revoke with the services, within what the granter holds::

    await grant_permission(actor, "catalog.change_product", product, user=ada.id)
    await revoke_permission(actor, "catalog.change_product", product, user=ada.id)

``HasObjectPermissions("catalog.change_product")`` then lets Ada change that
product, and ``visible_scope("view", Product)`` narrows a resource to what
each caller may see, in SQL.
"""

from .backends import ObjectPermissionBackend
from .criteria import Grants, VisibleTo, visible_scope, visible_to
from .models import (
    ObjectPermission,
    ObjectPermissionGranted,
    ObjectPermissionId,
    ObjectPermissionRevoked,
)
from .services import (
    InvalidGrant,
    grant_permission,
    grants_on,
    revoke_permission,
)
from .uow import (
    InMemoryObjectPermissions,
    ObjectPermissionRepository,
    ObjectPermissionsUnitOfWork,
)

__all__ = [
    "Grants",
    "InMemoryObjectPermissions",
    "InvalidGrant",
    "ObjectPermission",
    "ObjectPermissionBackend",
    "ObjectPermissionGranted",
    "ObjectPermissionId",
    "ObjectPermissionRepository",
    "ObjectPermissionRevoked",
    "ObjectPermissionsUnitOfWork",
    "VisibleTo",
    "grant_permission",
    "grants_on",
    "revoke_permission",
    "visible_scope",
    "visible_to",
]
