"""WEB-18: OpenAPI does not state a resource's conditional-request contract.

A resource with ``require_if_match=True`` answers every write without
``If-Match`` with ``428 precondition_required``, but its OpenAPI document
declares the header ``required: false`` (only its description says
"Required"), so a generated client omits it, and ``openapi --diff`` never
reports turning the requirement on as a breaking change. The ``ETag`` the
client must send back is not documented on any response either (nor
``Location`` on a 201), so a generated client has no typed way to read it.
"""

from typing import Any

from fastapi import FastAPI

from domainkit.http.errors import install_exception_handlers
from domainkit.http.openapi import breaking_changes, install_openapi
from domainkit.resources import ALL_FIELDS, ModelActions, ResourceRouter
from domainkit_tests.gadgets import FakeGadgetsUnitOfWork, Gadget, InMemoryGadgets
from domainkit_tests.test_resources import GADGETS


def schema(*, require_if_match: bool) -> dict[str, Any]:
    """The OpenAPI document of the gadget resource, generic writes on."""
    store = InMemoryGadgets([])
    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=lambda: FakeGadgetsUnitOfWork(store),
        repository=lambda uow: uow.gadgets,
        rule=ModelActions(GADGETS),
        crud=True,
        writable=ALL_FIELDS,
        require_if_match=require_if_match,
    )
    app = FastAPI()
    install_exception_handlers(app)
    install_openapi(app)
    app.include_router(resource)
    return app.openapi()


def test_a_required_if_match_is_a_required_parameter() -> None:
    """The header a write is refused without is required in the document."""
    document = schema(require_if_match=True)
    patch = document["paths"]["/gadgets/{gadget_id}"]["patch"]
    (if_match,) = [each for each in patch["parameters"] if each["name"] == "If-Match"]

    assert if_match["required"] is True
    assert breaking_changes(schema(require_if_match=False), document) != []


def test_reads_and_writes_document_the_etag_they_send() -> None:
    """``ETag`` on reads and writes, ``Location`` on a create."""
    paths = schema(require_if_match=True)["paths"]

    read = paths["/gadgets/{gadget_id}"]["get"]["responses"]["200"]
    changed = paths["/gadgets/{gadget_id}"]["patch"]["responses"]["200"]
    created = paths["/gadgets"]["post"]["responses"]["201"]

    assert "ETag" in read.get("headers", {})
    assert "ETag" in changed.get("headers", {})
    assert {"ETag", "Location"} <= set(created.get("headers", {}))
