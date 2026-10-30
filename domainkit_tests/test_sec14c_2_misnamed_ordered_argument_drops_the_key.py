"""SEC-14c-2: ``@ordered("name")`` naming no argument of its task drops the key.

``task_key`` reads ``arguments.get(name)``: a name the task does not have
(a typo, or an argument renamed later) gives ``None``, which the docs say
"leaves the message without a key". Nothing complains: not ``ordered``,
not ``@task``, not ``defer``/``enqueue``. Every message of the task then
runs unordered, in parallel and in any order, while the same mistake on a
durable handler fails loudly (``getattr`` on the event). The safe
behaviour: refuse the declaration (or the message) whose key name is not
an argument of the task.
"""

import pytest

from domainkit.contrib.messaging import enqueue, ordered, task
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.test_messaging import messaging, tables  # noqa: F401
from domainkit_tests.test_messaging_ordering import rows

pytestmark = pytest.mark.anyio


@pytest.mark.usefixtures("tables")
async def test_a_key_name_that_is_no_argument_of_the_task_is_refused() -> None:
    """The task's argument is ``account_id``; the key names ``acount_id``."""
    try:

        @task(name="tests.sec14c.post_entry")
        @ordered("acount_id")
        async def post_entry(account_id: str, amount: str) -> None:
            """Post to the account's ledger, one at a time per account."""

    except ImproperlyConfigured:
        return  # Refused where it is declared: safe.

    try:
        await enqueue(post_entry, account_id="acct-1", amount="10.00")
        await enqueue(post_entry, account_id="acct-1", amount="-10.00")
    except (ImproperlyConfigured, ValueError, TypeError):
        return  # Refused where it is queued: safe.

    found = [row for row in await rows() if row["target"] == "tests.sec14c.post_entry"]
    assert len(found) == 2
    # Unsafe on ac71361: both are unkeyed and pending, and run in parallel.
    assert [row["ordering_key"] for row in found] == ["acct-1", "acct-1"]
