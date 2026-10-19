"""Privacy requests and retention across installed domains (GDPR's rights).

Mark the fields of aggregates that hold personal data, with the field that
names whose it is, as events do::

    class Customer(AggregateRoot[CustomerId]):
        email: Annotated[str, Personal(of="id")]
        phone: Annotated[str | None, Personal(of="id")] = None

    class Order(AggregateRoot[OrderId]):
        customer_id: CustomerId
        delivery_address: Annotated[str | None, Personal(of="customer_id")]

Then, with ``domainkit.contrib.audit`` installed:

- ``manage.py privacy export --subject <id>`` writes every such field
  about one person, across every installed domain, as JSON (``export_subject``);
- ``manage.py privacy erase --subject <id>`` erases them through units of
  work, so versions, events, and the audit trail follow, and forgets the
  person's keys in the audit trail (``erase_subject``);
- ``manage.py privacy purge`` applies each domain's ``RETENTION``
  (``Retention`` policies): old rows are deleted, or their personal fields
  erased (``purge``).

Each operation is recorded in the audit trail. See ``docs/privacy.md`` and
ADR 0041.
"""

from .events import PersonalDataErased
from .models import PersonalModel, Policy, personal_models, problems, retention_policies
from .operations import (
    Purged,
    check_reason,
    check_subject,
    erase_subject,
    export_subject,
    purge,
)
from .retention import Retention

__all__ = [
    "PersonalDataErased",
    "PersonalModel",
    "Policy",
    "Purged",
    "Retention",
    "check_reason",
    "check_subject",
    "erase_subject",
    "export_subject",
    "personal_models",
    "problems",
    "purge",
    "retention_policies",
]
