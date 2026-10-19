"""The OpenAPI schema: stable operation ids, finishing touches, breaking changes.

``OPENAPI_OPERATION_IDS = "route_name"`` makes each operation id the route's
name (its function's name unless ``name=`` says otherwise), so moving a
router under another prefix does not rename the methods of generated
clients; FastAPI's own ids (``"fastapi"``, the default) also hold the path
and the method. Names must then be unique: ``check`` lists the clashes, and
staging and production refuse to start with any (see ADR 0031).

``finish_schema`` marks deprecated operations (``domainkit.http.versioning``)
and, with ``ERROR_FORMAT = "problem+json"``, documents errors as problem
details. ``breaking_changes`` compares two schemas, for ``manage.py openapi
--diff``.
"""

import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.utils import generate_unique_id

from domainkit.conf import settings
from domainkit.http.errors import (
    PROBLEM_FORMAT,
    PROBLEM_MEDIA_TYPE,
    ErrorResponse,
    ProblemDetails,
)
from domainkit.http.versioning import Deprecation

METHODS: Final = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
"""The operations of an OpenAPI path item."""
JSON: Final = "application/json"
COMPONENTS: Final = "#/components/schemas/"
MAX_DEPTH: Final = 64
"""How deep ``breaking_changes`` follows nested schemas."""
DEPRECATION_HEADERS: Final = {
    "Deprecation": "When the operation was deprecated (RFC 9745), as @<seconds>.",
    "Sunset": "When the operation stops answering (RFC 8594).",
    "Link": 'Where the deprecation is described (rel="deprecation").',
}


def operation_id(route: APIRoute) -> str:
    """A route's operation id, as ``OPENAPI_OPERATION_IDS`` asks."""
    if settings.OPENAPI_OPERATION_IDS == "route_name":
        return route.name
    return generate_unique_id(route)


def install_openapi(app: FastAPI) -> None:
    """Make ``app.openapi()`` return the finished schema (``finish_schema``)."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            return finish_schema(app, FastAPI.openapi(app))
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def finish_schema(app: FastAPI, schema: dict[str, Any]) -> dict[str, Any]:
    """Mark deprecated operations, and document problem details if in use."""
    paths: dict[str, Any] = schema.get("paths", {})
    for path, method, dependant in _operations(app):
        operation = paths.get(path, {}).get(method)
        deprecation = _deprecation(dependant)
        if operation is not None and deprecation is not None:
            _deprecate(operation, deprecation)
    if settings.ERROR_FORMAT == PROBLEM_FORMAT:
        _document_problems(schema)
    return schema


def duplicate_operation_ids(app: FastAPI) -> list[str]:
    """Operation ids that more than one route of ``app`` has.

    A route answering several methods has one id, as in FastAPI, which
    warns about it itself.
    """
    seen: dict[str, list[str]] = {}
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        if not context.include_in_schema:
            continue
        identifier = context.operation_id or context.unique_id
        methods = " ".join(sorted(context.methods or ()))
        seen.setdefault(identifier, []).append(f"{methods} {context.path_format}")
    return [
        f"Operation id {identifier!r} names {' and '.join(routes)}; give the "
        "routes distinct names (name=...)."
        for identifier, routes in seen.items()
        if len(routes) > 1
    ]


def _operations(app: FastAPI) -> Iterator[tuple[str, str, Dependant]]:
    """``(path, method, dependant)`` of every documented operation."""
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        path, dependant = context.path_format, context.dependant
        for method in sorted(context.methods or ()):
            yield str(path), method.lower(), dependant


def _deprecation(dependant: Dependant) -> Deprecation | None:
    """The innermost ``Deprecation`` among the dependencies, if any."""
    found = None
    for dependency in dependant.dependencies:
        if isinstance(dependency.call, Deprecation):
            found = dependency.call
        found = _deprecation(dependency) or found
    return found


def _deprecate(operation: dict[str, Any], deprecation: Deprecation) -> None:
    """Mark ``operation`` deprecated and document the headers it sends."""
    operation["deprecated"] = True
    for name, value in deprecation.openapi().items():
        operation.setdefault(name, value)
    sent = {name for name, _ in deprecation.headers()}
    for response in operation.get("responses", {}).values():
        headers = response.setdefault("headers", {})
        for name, description in DEPRECATION_HEADERS.items():
            if name in sent:
                headers.setdefault(
                    name, {"description": description, "schema": {"type": "string"}}
                )


def _document_problems(schema: dict[str, Any]) -> None:
    """Document error responses as ``application/problem+json`` problem details."""
    error_ref = f"{COMPONENTS}{ErrorResponse.__name__}"
    problem_ref = f"{COMPONENTS}{ProblemDetails.__name__}"
    for path_item in schema.get("paths", {}).values():
        for method in METHODS:
            responses = path_item.get(method, {}).get("responses", {})
            for response in responses.values():
                content = response.get("content", {})
                body = content.get(JSON)
                if body is not None and body.get("schema") == {"$ref": error_ref}:
                    del content[JSON]
                    content[PROBLEM_MEDIA_TYPE] = {"schema": {"$ref": problem_ref}}
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    generated = ProblemDetails.model_json_schema(ref_template=f"{COMPONENTS}{{model}}")
    components.update(generated.pop("$defs", {}))
    components[ProblemDetails.__name__] = generated
    if error_ref not in json.dumps(schema):
        components.pop(ErrorResponse.__name__, None)


# Breaking changes ---------------------------------------------------------------


@dataclass(slots=True)
class _Comparison:
    """Two schemas being compared, and what was found."""

    old: Mapping[str, Any]
    new: Mapping[str, Any]
    found: list[str] = field(default_factory=list)
    seen: set[tuple[str, str, str]] = field(default_factory=set)


def breaking_changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """What in ``new`` breaks clients written against ``old``.

    Breaking: a removed path or operation, a success status no longer
    answered, a new required parameter, body, or body field, a request
    value accepted before and refused now (a narrower type, fewer enum
    values, tighter bounds, no other fields), and a response field removed,
    no longer always present, or of a wider type or more enum values.
    """
    comparison = _Comparison(old, new)
    new_paths = {_template(path): item for path, item in _items(new, "paths")}
    for path, old_item in _items(old, "paths"):
        new_item = new_paths.get(_template(path))
        if new_item is None:
            comparison.found.append(f"{path}: removed")
            continue
        for method in METHODS:
            old_operation = old_item.get(method)
            if not isinstance(old_operation, dict):
                continue
            new_operation = new_item.get(method)
            where = f"{method.upper()} {path}"
            if not isinstance(new_operation, dict):
                comparison.found.append(f"{where}: removed")
                continue
            _compare_operation(comparison, where, old_operation, new_operation)
    return comparison.found


def _items(document: Mapping[str, Any], key: str) -> list[tuple[str, Any]]:
    """The entries of ``document[key]`` that are objects."""
    value = document.get(key)
    if not isinstance(value, dict):
        return []
    return [(name, item) for name, item in value.items() if isinstance(item, dict)]


def _template(path: str) -> str:
    """A path with its parameters unnamed: renaming one breaks nothing."""
    parts = path.split("/")
    return "/".join("{}" if part.startswith("{") else part for part in parts)


def _compare_operation(
    comparison: _Comparison,
    where: str,
    old: dict[str, Any],
    new: dict[str, Any],
) -> None:
    """Parameters, the request body, and the success responses."""
    old_parameters = _parameters(comparison.old, old)
    for key, parameter in _parameters(comparison.new, new).items():
        before = old_parameters.get(key)
        place, name = key
        if parameter.get("required") and not (before or {}).get("required"):
            comparison.found.append(f"{where}: new required {place} parameter {name!r}")
        if before is not None:
            _compare_request(
                comparison,
                f"{where}: {place} parameter {name!r}",
                before.get("schema", {}),
                parameter.get("schema", {}),
                0,
            )
    _compare_body(comparison, where, old, new)
    old_responses = old.get("responses", {})
    new_responses = new.get("responses", {})
    for status, response in old_responses.items():
        if not str(status).startswith("2"):
            continue
        if status not in new_responses:
            comparison.found.append(f"{where}: no longer answers {status}")
            continue
        old_schema = _json_schema(comparison.old, response)
        new_schema = _json_schema(comparison.new, new_responses[status])
        if old_schema is not None and new_schema is not None:
            _compare_response(
                comparison, f"{where} {status}", old_schema, new_schema, 0
            )


def _parameters(
    document: Mapping[str, Any], operation: dict[str, Any]
) -> dict[tuple[str, str], dict[str, Any]]:
    """The operation's parameters by ``(in, name)``, references followed."""
    found = {}
    for parameter in operation.get("parameters", []):
        resolved = _resolve(document, parameter)
        if "name" in resolved and "in" in resolved:
            found[resolved["in"], resolved["name"]] = resolved
    return found


def _compare_body(
    comparison: _Comparison, where: str, old: dict[str, Any], new: dict[str, Any]
) -> None:
    """A body newly required, or a media type or value no longer accepted."""
    old_body = _resolve(comparison.old, old.get("requestBody", {}))
    new_body = _resolve(comparison.new, new.get("requestBody", {}))
    if new_body.get("required") and not old_body.get("required"):
        comparison.found.append(f"{where}: the request body is now required")
    new_content = new_body.get("content", {})
    for media_type, old_media in old_body.get("content", {}).items():
        new_media = new_content.get(media_type)
        if new_media is None:
            if new_content:
                comparison.found.append(f"{where}: no longer accepts {media_type}")
            continue
        _compare_request(
            comparison,
            f"{where}: request body",
            old_media.get("schema", {}),
            new_media.get("schema", {}),
            0,
        )


def _json_schema(
    document: Mapping[str, Any], response: dict[str, Any]
) -> dict[str, Any] | None:
    """The JSON schema of a response, if it has one."""
    content = _resolve(document, response).get("content", {})
    media = content.get(JSON) or content.get(PROBLEM_MEDIA_TYPE)
    return None if media is None else media.get("schema", {})


def _resolve(document: Mapping[str, Any], item: object) -> dict[str, Any]:
    """``item`` with its local ``$ref`` followed (``#/components/...``)."""
    seen: set[str] = set()
    while isinstance(item, dict) and isinstance(ref := item.get("$ref"), str):
        if ref in seen or not ref.startswith("#/"):
            return {}
        seen.add(ref)
        item = _pointer(document, ref)
    return item if isinstance(item, dict) else {}


def _pointer(document: Mapping[str, Any], ref: str) -> object:
    """What a local JSON pointer points at, or None."""
    found: object = document
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(found, dict) or key not in found:
            return None
        found = found[key]
    return found


def _first_visit(
    comparison: _Comparison, side: str, old: object, new: object, depth: int
) -> bool:
    """Whether to compare these schemas: not too deep, not a cycle's repeat."""
    if depth > MAX_DEPTH:
        return False
    old_ref = old.get("$ref") if isinstance(old, dict) else None
    new_ref = new.get("$ref") if isinstance(new, dict) else None
    if old_ref is None or new_ref is None:
        return True
    pair = (side, str(old_ref), str(new_ref))
    if pair in comparison.seen:
        return False
    comparison.seen.add(pair)
    return True


def _compare_request(
    comparison: _Comparison, where: str, old: object, new: object, depth: int
) -> None:
    """Report values ``old`` accepted that ``new`` refuses."""
    if not _first_visit(comparison, "request", old, new, depth):
        return
    before = _resolve(comparison.old, old)
    after = _resolve(comparison.new, new)
    found = comparison.found
    old_types, new_types = _types(comparison.old, before), _types(comparison.new, after)
    if lost := _uncovered(old_types, new_types):
        found.append(f"{where} no longer accepts {_kinds(lost)}")
    old_enum, new_enum = _enum(before), _enum(after)
    if new_enum is not None and (old_enum is None or not old_enum <= new_enum):
        removed = "other values" if old_enum is None else _listed(old_enum - new_enum)
        found.append(f"{where} no longer accepts {removed}")
    for bound, tighter in _BOUNDS.items():
        if _tightened(before.get(bound), after.get(bound), tighter):
            found.append(f"{where} has a tighter {bound} ({after[bound]!r})")
    old_properties = _properties(comparison.old, before)
    new_properties = _properties(comparison.new, after)
    old_required = set(before.get("required", ()))
    for name in sorted(set(after.get("required", ())) - old_required):
        found.append(f"{where} field {name!r} is now required")
    if after.get("additionalProperties") is False:
        if before.get("additionalProperties", True) is not False:
            found.append(f"{where} no longer accepts fields it does not declare")
        for name in sorted(set(old_properties) - set(new_properties)):
            found.append(f"{where} no longer accepts field {name!r}")
    for name, schema in new_properties.items():
        if name in old_properties:
            _compare_request(
                comparison,
                f"{where} field {name!r}",
                old_properties[name],
                schema,
                depth + 1,
            )
    _compare_items(comparison, where, before, after, depth, _compare_request)


def _compare_response(
    comparison: _Comparison, where: str, old: object, new: object, depth: int
) -> None:
    """Report what clients of ``old`` relied on that ``new`` takes away."""
    if not _first_visit(comparison, "response", old, new, depth):
        return
    before = _resolve(comparison.old, old)
    after = _resolve(comparison.new, new)
    found = comparison.found
    old_types, new_types = _types(comparison.old, before), _types(comparison.new, after)
    if added := _uncovered(new_types, old_types):
        found.append(f"{where} may now be {_kinds(added)}")
    old_enum, new_enum = _enum(before), _enum(after)
    if old_enum is not None and (new_enum is None or not new_enum <= old_enum):
        added_values = (
            "other values" if new_enum is None else _listed(new_enum - old_enum)
        )
        found.append(f"{where} may now be {added_values}")
    old_properties = _properties(comparison.old, before)
    new_properties = _properties(comparison.new, after)
    new_required = set(after.get("required", ()))
    for name in sorted(old_properties):
        if name not in new_properties:
            found.append(f"{where}: removed field {name!r}")
            continue
        if name in set(before.get("required", ())) - new_required:
            found.append(f"{where}: field {name!r} is no longer always present")
        _compare_response(
            comparison,
            f"{where} field {name!r}",
            old_properties[name],
            new_properties[name],
            depth + 1,
        )
    _compare_items(comparison, where, before, after, depth, _compare_response)


_BOUNDS: Final[dict[str, Callable[[float, float], float]]] = {
    "maxLength": min,
    "minLength": max,
    "maximum": min,
    "minimum": max,
    "exclusiveMaximum": min,
    "exclusiveMinimum": max,
    "maxItems": min,
    "minItems": max,
}
"""Each bound, and which of two values of it is the tighter one."""


def _tightened(
    old: object, new: object, tighter: Callable[[float, float], float]
) -> bool:
    """Whether bound ``new`` refuses values bound ``old`` accepted."""
    if not isinstance(new, int | float) or isinstance(new, bool):
        return False
    if not isinstance(old, int | float) or isinstance(old, bool):
        return True
    return new != old and tighter(old, new) == new


def _compare_items(
    comparison: _Comparison,
    where: str,
    before: dict[str, Any],
    after: dict[str, Any],
    depth: int,
    compare: Any,  # noqa: ANN401 - _compare_request or _compare_response.
) -> None:
    """Compare the items of two array schemas."""
    if isinstance(before.get("items"), dict) and isinstance(after.get("items"), dict):
        compare(comparison, f"{where}[]", before["items"], after["items"], depth + 1)


def _types(
    document: Mapping[str, Any], schema: dict[str, Any], depth: int = 0
) -> set[str]:
    """The JSON types ``schema`` allows; ``{"any"}`` when it does not say.

    Compositions nested past ``MAX_DEPTH`` (a schema made of itself) allow
    anything.
    """
    if depth > MAX_DEPTH:
        return {"any"}
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {str(each) for each in declared}
    for key in ("anyOf", "oneOf"):
        members = schema.get(key)
        if isinstance(members, list) and members:
            return set().union(
                *(
                    _types(document, _resolve(document, member), depth + 1)
                    for member in members
                )
            )
    if "const" in schema:
        return {_type_of(schema["const"])}
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return {_type_of(value) for value in schema["enum"]}
    members = schema.get("allOf")
    if isinstance(members, list) and len(members) == 1:
        return _types(document, _resolve(document, members[0]), depth + 1)
    if "properties" in schema:
        return {"object"}
    return {"any"}


def _type_of(value: object) -> str:
    """The JSON type of a value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _uncovered(wanted: set[str], offered: set[str]) -> set[str]:
    """The types in ``wanted`` that ``offered`` does not allow.

    ``any`` allows everything, and ``number`` allows integers.
    """
    if "any" in offered:
        return set()
    if "any" in wanted:
        return {"any"} if offered else set()
    covered = offered | ({"integer"} if "number" in offered else set())
    return wanted - covered


def _enum(schema: dict[str, Any]) -> set[str] | None:
    """The values ``schema`` lists (as JSON), if it lists any."""
    if "const" in schema:
        return {json.dumps(schema["const"])}
    values = schema.get("enum")
    if not isinstance(values, list):
        return None
    return {json.dumps(value) for value in values}


def _properties(
    document: Mapping[str, Any], schema: dict[str, Any], depth: int = 0
) -> dict[str, Any]:
    """An object schema's properties, those of a single ``allOf`` member too."""
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return properties
    members = schema.get("allOf")
    if isinstance(members, list) and len(members) == 1 and depth < MAX_DEPTH:
        return _properties(document, _resolve(document, members[0]), depth + 1)
    return {}


def _listed(values: Iterable[object]) -> str:
    """Values for a message, sorted and comma-separated."""
    return ", ".join(sorted(str(value) for value in values))


def _kinds(types: set[str]) -> str:
    """JSON types for a message; ``any`` reads as other types."""
    return _listed("other types" if kind == "any" else kind for kind in types)
