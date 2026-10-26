"""Generic ``POST``, ``PATCH``, and ``DELETE`` that set fields directly (``crud=True``).

For reference data, where a use case per change would say nothing more:
they record ``RecordCreated``, ``RecordChanged``, and ``RecordDeleted``.
Only plain fields are set, never the id, the version, a relation, or the
parent's field; a ``FieldPolicy`` keeps fields from callers without its
permissions, and a write may not leave the caller's reach.
"""

import dataclasses
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from fastapi import Response, status
from pydantic import BaseModel

from domainkit.ddd.fields import FieldInfo, fields_of

from ..access import ADD, CHANGE, DELETE
from ..events import RecordChanged, RecordCreated, RecordDeleted
from ..schemas import GENERATED, PatchModel, schema_for
from . import presenting, reach
from .context import DEFAULT, Aggregate, RouteContext, RouteSpec
from .declaring import add_route
from .values import changes, model_label, to_domain

if TYPE_CHECKING:
    from . import ResourceRouter


class CrudRoutes:
    """The three generic writes of a resource (see the module)."""

    def __init__(
        self,
        router: "ResourceRouter",
        create: type[BaseModel] | None,
        update: type[PatchModel] | None,
        new_id: Callable[[], object],
    ) -> None:
        """Derive the schemas the writes take, and check given ones.

        Raises:
            TypeError: A schema sets what generic writes do not, or the
                create schema lacks a field the model needs.
        """
        model = router.model
        self.router = router
        self.model = model
        self.fields = fields_of(model)
        self.new_id = new_id
        read_only = (
            router.policy.read_only if router.policy is not None else frozenset()
        )
        relations = [
            name for name, field in self.fields.items() if field.entity is not None
        ]
        # The parent's field: the path gives it, and no write moves it.
        self.pinned = [router.parent_field] if router.parent_field is not None else []
        fixed = [
            *relations,
            *self.pinned,
            *(name for name in read_only if name in self.fields),
        ]
        self.create = create or schema_for(model, write=True, exclude=fixed)
        self.update = update or schema_for(model, partial=True, exclude=fixed)
        for schema in (self.create, self.update):
            _check_plain(model, schema, self.fields, self.pinned)
        needed = {
            field.name
            for field in dataclasses.fields(model)
            if field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING
            and field.name not in {"id", *self.pinned}
        }
        if missing := sorted(needed - set(self.create.model_fields)):
            raise TypeError(
                f"The create schema lacks {model.__name__}'s {', '.join(missing)}."
            )
        self.label = model_label(model)
        self.changing = reach.rules_of(router, CHANGE)
        self.deleting = reach.rules_of(router, DELETE)

    def declare(self) -> None:
        """Add the three routes to the resource."""
        router = self.router
        policy = router.policy
        label = router.basename.replace("_", " ")
        add_route(
            router,
            RouteSpec(
                "",
                "POST",
                f"create_{router.basename}",
                f"Create a {label}",
                ADD,
                body=policy.describe_changes(self.create) if policy else self.create,
                returns=router.schema,
                status_code=status.HTTP_201_CREATED,
                writes=True,
            ),
            self.created,
        )
        add_route(
            router,
            RouteSpec(
                f"/{{{router.lookup}}}",
                "PATCH",
                f"update_{router.basename}",
                f"Change fields of a {label}",
                CHANGE,
                detail=True,
                body=(
                    policy.describe_changes(self.update)
                    if policy is not None
                    else self.update
                ),
                returns=router.schema,
                writes=True,
            ),
            self.changed,
        )
        add_route(
            router,
            RouteSpec(
                f"/{{{router.lookup}}}",
                "DELETE",
                f"delete_{router.basename}",
                f"Delete a {label}",
                DELETE,
                detail=True,
                returns=None,
                status_code=status.HTTP_204_NO_CONTENT,
                writes=True,
            ),
            self.deleted,
        )

    async def created(self, context: RouteContext) -> Response:
        """``POST``: a new aggregate of the body's fields; ``201`` with ``Location``."""
        router = self.router
        request = context.request
        payload = context.body
        assert payload is not None  # noqa: S101 - the route takes a body.
        if router.policy is not None:
            await router.policy.check_changes(request, payload.model_fields_set)
        given = payload.model_dump()
        given |= {
            name: context.ids[reach.path_name(router, name)] for name in self.pinned
        }
        aggregate = self.model(
            id=self.new_id(),
            **{
                name: to_domain(self.fields[name], value)
                for name, value in given.items()
            },
        )
        aggregate.record(
            RecordCreated(
                model=self.label, record_id=str(aggregate.id), fields=tuple(given)
            )
        )
        async with context.uow as uow:
            await router.repository(uow).add(aggregate)
            await reach.keep_in_reach(router, uow, request, context.ids, aggregate.id)
            await uow.commit()
        return await presenting.answer(router, context, aggregate, DEFAULT, 201)

    async def changed(self, context: RouteContext) -> Response:
        """``PATCH``: set the fields the body sets; record those that changed."""
        router = self.router
        request = context.request
        given = changes(context.body)
        if router.policy is not None:
            await router.policy.check_changes(request, given)

        async def apply() -> Aggregate:
            async with context.uow as uow:
                aggregate = await reach.load(router, uow, request, context.ids)
                await reach.check_object(request, aggregate, self.changing)
                names = []
                for name, value in given.items():
                    new = to_domain(self.fields[name], value)
                    if getattr(aggregate, name) != new:
                        setattr(aggregate, name, new)
                        names.append(name)
                if names:
                    aggregate.record(
                        RecordChanged(
                            model=self.label,
                            record_id=str(aggregate.id),
                            fields=tuple(names),
                        )
                    )
                    await reach.keep_in_reach(
                        router, uow, request, context.ids, aggregate.id
                    )
                await uow.commit()
            return aggregate

        aggregate = await reach.conditionally(router, context, apply)
        return await presenting.answer(router, context, aggregate, DEFAULT, 200)

    async def deleted(self, context: RouteContext) -> Response:
        """``DELETE``: remove the aggregate; ``204``."""
        router = self.router
        request = context.request

        async def apply() -> None:
            async with context.uow as uow:
                aggregate = await reach.load(router, uow, request, context.ids)
                await reach.check_object(request, aggregate, self.deleting)
                aggregate.record(
                    RecordDeleted(model=self.label, record_id=str(aggregate.id))
                )
                await router.repository(uow).remove(aggregate)
                await uow.commit()

        await reach.conditionally(router, context, apply)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


def _check_plain(
    model: type,
    schema: type[BaseModel],
    fields: Mapping[str, FieldInfo],
    pinned: list[str],
) -> None:
    """Refuse a schema that sets more than plain fields of ``model``.

    Raises:
        TypeError: It sets an unknown or generated field, a relation, or the
            parent's field.
    """
    if bad := sorted(
        name
        for name in schema.model_fields
        if name not in fields
        or name in GENERATED
        or name in pinned
        or fields[name].entity is not None
    ):
        raise TypeError(
            f"crud=True sets plain fields of {model.__name__}, not {', '.join(bad)}."
        )
