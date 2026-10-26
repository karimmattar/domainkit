"""REST resources over aggregates: routes, schemas, conditional writes, policies.

DRF's viewsets, routers, and serializers, for task-based commands:

- ``ResourceRouter``: list and read an aggregate (filters, ordering, search,
  pages, ``ETag``), run commands on it through application services
  (``command_route``), answer queries (``query_route``), nest resources
  (``parent=``), and, for reference data, generic create, change, and
  delete (``crud=True``).
- ``schema_for``: pydantic schemas derived from domain classes;
  ``PatchModel`` for partial changes (``UNSET`` for what was left out).
- ``ModelActions``: Django's model permissions by each route's action.
- ``FieldPolicy``: who sees and who changes each field.
- ``If-None-Match`` answers ``304``; ``If-Match`` makes writes conditional
  (``412``), and a resource may require it (``428``).
- ``batch_route``: one command for many items, each answered (``207``),
  atomically on request; ``exports=`` streams listings as CSV or NDJSON.

See ``docs/resources.md``.
"""

from domainkit.ddd.messages import UNSET, Unset

from .access import Actions, HasModelPermission, ModelActions
from .batches import BatchAnswer, BatchError, BatchResult
from .conditional import PreconditionRequired, entity_tag, expected_versions
from .events import RecordChanged, RecordCreated, RecordDeleted
from .policies import FieldNotEditable, FieldPolicy
from .renderers import ExportTooLarge, NotAcceptable
from .routing import ALL_FIELDS, MERGE_PATCH, Operation, ResourceRouter, to_domain
from .schemas import DomainSchema, PatchModel, schema_for

__all__ = [
    "ALL_FIELDS",
    "MERGE_PATCH",
    "UNSET",
    "Actions",
    "BatchAnswer",
    "BatchError",
    "BatchResult",
    "DomainSchema",
    "ExportTooLarge",
    "FieldNotEditable",
    "FieldPolicy",
    "HasModelPermission",
    "ModelActions",
    "NotAcceptable",
    "Operation",
    "PatchModel",
    "PreconditionRequired",
    "RecordChanged",
    "RecordCreated",
    "RecordDeleted",
    "ResourceRouter",
    "Unset",
    "entity_tag",
    "expected_versions",
    "schema_for",
    "to_domain",
]
