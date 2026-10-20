"""A generic admin over REST: Django's ``admin.site.register``, without HTML.

Register an aggregate, and the admin serves it at
``/<domain>/<plural>`` under the prefix it is mounted at: a page of items
(``list_display``, ``filters``, ``search``, ``ordering``), each item with
its ``ETag``, the commands and queries given, and each item's history from
the audit trail. ``GET /meta`` describes all of it, as far as the caller
may use it, for a generic client to render::

    from domainkit.contrib import admin

    admin.register(
        Product,
        unit_of_work=get_unit_of_work,
        repository=lambda uow: uow.products,
        list_display=["name", "archived"],
        search=["name"],
        commands=[
            admin.command(
                ArchiveProduct, service=services.archive_product, path="/archive"
            ),
        ],
    )

    # config/urls.py, after the modules that register:
    router.include_router(admin.site.router, prefix="/admin")

Every route needs staff (``is_staff``) or a superuser, with credentials
not limited to scopes, and the model permission of its action
(``catalog.view_product`` to list and read, ``change`` for commands,
``audit.view_history`` too for histories).
Actions are commands run by application services: the admin never sets
fields itself. Users, groups, and permissions keep their own admin
(``domainkit.contrib.auth.admin``), which keeps grant ceilings and the
last superuser; the site refuses them.
"""

from .sites import (
    AdminCommand,
    AdminMeta,
    AdminQuery,
    AdminSite,
    ModelAdmin,
    command,
    query,
)

site = AdminSite()
"""The default site; ``register`` registers with it."""
register = site.register

__all__ = [
    "AdminCommand",
    "AdminMeta",
    "AdminQuery",
    "AdminSite",
    "ModelAdmin",
    "command",
    "query",
    "register",
    "site",
]
