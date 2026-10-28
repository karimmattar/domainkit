"""SEC-12a-5: a subject's export drops what one of a domain's hooks gave.

ADR 0101: a domain's several hooks of one kind keep its one identity, "its
erasures are summed into one audit record, and its exports merged under
its label". ``export_subject`` merges with ``dict.update``: when two hooks
of a domain name the same kind, as two stores of deliveries do (or a
registered hook and the deprecated ``export_subject`` override it still
calls), the second replaces the first, silently. The erasure of the same
kind is summed, so the person's copies are erased, and counted, while the
export (the right of access, GDPR Art. 15) leaves out those of the first
hook. Every hook's copies must be in the export, or the clash refused.
"""

import json

from domainkit.apps.hooks import on_export_subject
from domainkit.contrib.privacy import export_subject
from domainkit.management.base import run_async
from domainkit_tests.test_privacy import clinic  # noqa: F401 - fixture.


def test_every_hooks_copies_are_in_the_export(
    clinic: str,  # noqa: F811 - the fixture.
) -> None:
    """Two stores of one domain, each with a delivery to the subject."""

    async def outbound(subject: str) -> dict[str, object]:
        return {"deliveries": [{"store": "outbound", "to": subject}]}

    async def retried(subject: str) -> dict[str, object]:
        return {"deliveries": [{"store": "retried", "to": subject}]}

    on_export_subject(outbound, domain="alpha")
    on_export_subject(retried, domain="alpha")

    exported = json.dumps(run_async(export_subject("nobody"))["copies"])

    assert '"outbound"' in exported, f"the first store's copy is missing: {exported}"
