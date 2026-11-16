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

A grant may end (``expires_at=``); ``cleargrants`` deletes those past it.
A role grants several permissions at once, and takes them back together::

    EDITOR = ObjectRole("editor", ["catalog.view_product", "catalog.change_product"])
    await grant_role(actor, EDITOR, product, user=ada.id, expires_at=friday)

A permission of another model is granted on objects of a model only when
a domain says it applies there: ``grantable_on(Company,
"memberships.add_membership")`` in its ``ready()``.
"""

from .backends import ObjectPermissionBackend
from .criteria import Grants, VisibleTo, visible_scope, visible_to
from .models import (
    ObjectPermission,
    ObjectPermissionGranted,
    ObjectPermissionId,
    ObjectPermissionRevoked,
    ObjectRole,
)
from .services import (
    InvalidGrant,
    clear_expired_grants,
    grant_permission,
    grant_role,
    grantable_on,
    grants_on,
    revoke_permission,
    revoke_role,
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
    "ObjectRole",
    "VisibleTo",
    "clear_expired_grants",
    "grant_permission",
    "grant_role",
    "grantable_on",
    "grants_on",
    "revoke_permission",
    "revoke_role",
    "visible_scope",
    "visible_to",
]
