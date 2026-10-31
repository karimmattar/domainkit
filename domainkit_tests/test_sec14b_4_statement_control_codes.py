"""SEC-14b-4: statement references and descriptions keep control codes.

An entry's reference refuses control characters (``REFERENCE``), and a
statement line's id does (``EXTERNAL_ID``), but ``CsvStatement`` stores a
line's ``reference`` and ``description`` as the file has them. A
statement (a bank's export, or a file someone hands the operator) can
carry terminal escapes that ``manage.py accounting unmatched`` writes
raw to the operator's terminal, and that ``GET /accounts/{id}/breaks``
serves. Fails on ``ac71361``.
"""

import io

import pytest

from domainkit.contrib.accounting import InvalidStatement
from domainkit.contrib.accounting.reconciliation import (
    CsvStatement,
    breaks_of,
    import_statement,
    reconcile,
)
from domainkit_tests.test_accounting import account, books  # noqa: F401 - fixture.

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]
ESCAPE = "\x1b]0;owned\x07\x1b[2J\x1b[1A\rLooks fine"


async def test_control_codes_in_a_statement_are_not_kept() -> None:
    """Refused (``invalid_statement``), or stored without them; never kept raw."""
    bank = await account("1000")
    buffer = io.StringIO()
    buffer.write("id,date,amount,currency,reference,description\n")
    buffer.write(f'b-1,2026-10-01,1.00,USD,"{ESCAPE}","{ESCAPE}"\n')
    try:
        await import_statement(bank.id, CsvStatement(buffer.getvalue()))
    except InvalidStatement:  # Refusing the line is safe.
        return
    await reconcile(bank.id)
    (found,) = await breaks_of(bank.id)
    assert not any(ord(char) < 0x20 or ord(char) == 0x7F for char in found.reference)
