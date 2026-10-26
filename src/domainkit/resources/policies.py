"""Field policies: which fields of a resource a caller sees and may change.

The user model's ``EDITABLE_FIELDS``, ``READ_ONLY_FIELDS``, and
``PROTECTED_FIELDS`` for any aggregate, per permission::

    policy = FieldPolicy(
        read_only=("created_at",),
        visible={"cost": "billing.view_cost"},
        editable={"discount": "billing.change_discount"},
    )
    ResourceRouter("/invoices", ..., policy=policy)

- A field in ``visible`` is shown only to callers granted its permission;
  others get the representation without it (the key is absent). OpenAPI
  marks it optional and names the permission.
- A field in ``editable`` is changed only by callers granted its
  permission; a partial change (``PATCH``) or a generic create that sets
  it is refused otherwise (403 ``field_not_editable``). So is one that
  sets a field in ``visible`` the caller may not see.
- A ``read_only`` field is never set by a partial change or a generic
  create: it keeps its default.

Fields left out of a schema are neither shown nor changed at all, which
is how protected fields stay protected. Task commands are not partial
changes: what a command may change is the command's business, so the
policy applies only to ``PatchModel`` bodies and generic writes.
"""

from collections.abc import Collection, Iterable, Mapping
from typing import Any

from pydantic import BaseModel, create_model
from pydantic.fields import FieldInfo
from starlette.requests import HTTPConnection

from domainkit.ddd import PermissionDenied
from domainkit.http.access import HasPermissions, current_user


class FieldNotEditable(PermissionDenied):
    """A partial change sets a field the caller may not change."""


class FieldPolicy:
    """Who sees and who changes each field of a resource (see the module)."""

    def __init__(
        self,
        *,
        read_only: Collection[str] = (),
        visible: Mapping[str, str] | None = None,
        editable: Mapping[str, str] | None = None,
    ) -> None:
        """Hold the fields' permissions.

        Raises:
            ValueError: A field is both read-only and editable by permission.
        """
        self.read_only = frozenset(read_only)
        self.visible = dict(visible or {})
        self.editable = dict(editable or {})
        if both := sorted(self.read_only & set(self.editable)):
            raise ValueError(
                f"Fields cannot be read-only and editable: {', '.join(both)}."
            )

    def check(self, fields: Iterable[str], where: str) -> None:
        """Refuse a policy that names fields ``where`` lacks.

        Raises:
            TypeError: Some field is unknown.
        """
        named = self.read_only | set(self.visible) | set(self.editable)
        if unknown := sorted(named - set(fields)):
            raise TypeError(f"The field policy names fields {where} lacks: {unknown}.")

    async def hidden(self, request: HTTPConnection) -> frozenset[str]:
        """The fields the caller may not see."""
        if not self.visible:
            return frozenset()
        user = current_user(request)
        return frozenset(
            [
                name
                for name, permission in self.visible.items()
                if not await HasPermissions(permission).has_permission(request, user)
            ]
        )

    async def check_changes(
        self, request: HTTPConnection, names: Iterable[str]
    ) -> None:
        """Refuse to set fields the caller may not change, or may not see.

        Raises:
            FieldNotEditable: A field is read-only, is hidden from the
                caller, or needs a permission the caller lacks.
        """
        names = set(names)
        if names & self.read_only:
            raise FieldNotEditable("Some of these fields cannot be changed.")
        # What the caller may not see, they may not overwrite blind either.
        needed = {
            *(self.editable[name] for name in names if name in self.editable),
            *(self.visible[name] for name in names if name in self.visible),
        }
        user = current_user(request)
        for permission in sorted(needed):
            if not await HasPermissions(permission).has_permission(request, user):
                raise FieldNotEditable(
                    "You do not have permission to change some of these fields."
                )

    def document(self, schema: type[BaseModel]) -> type[BaseModel]:
        """``schema``, with each field only some callers see marked optional.

        The name stays, so OpenAPI shows one schema; the description says
        which permission shows the field.
        """
        return _described(
            schema,
            self.visible,
            "Shown only to users granted {}.",
            default=None,
            json_schema_extra=_no_default,
        )

    def describe_changes(self, schema: type[BaseModel]) -> type[BaseModel]:
        """A write's ``schema``, with the permissions setting each field needs."""
        needed = {
            name: " and ".join(
                sorted(
                    {
                        *([self.editable[name]] if name in self.editable else []),
                        *([self.visible[name]] if name in self.visible else []),
                    }
                )
            )
            for name in {*self.editable, *self.visible}
        }
        return _described(schema, needed, "Changed only by users granted {}.")


def _described(
    schema: type[BaseModel],
    permissions: Mapping[str, str],
    note: str,
    **options: Any,  # noqa: ANN401 - FieldInfo's options.
) -> type[BaseModel]:
    """``schema``, with ``note`` and ``options`` on each field ``permissions`` names."""
    named = [name for name in schema.model_fields if name in permissions]
    if not named:
        return schema
    fields: dict[str, Any] = {}
    for name in named:
        field = schema.model_fields[name]
        text = note.format(permissions[name])
        description = f"{field.description} {text}" if field.description else text
        fields[name] = (
            field.annotation,
            FieldInfo.merge_field_infos(field, description=description, **options),
        )
    return create_model(schema.__name__, __base__=schema, **fields)


def _no_default(schema: dict[str, Any]) -> None:
    """Leave the placeholder default out of OpenAPI: the field is absent, not null."""
    schema.pop("default", None)
