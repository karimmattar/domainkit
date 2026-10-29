"""SEC-13b-3: memory ledgers and storages keep one test's records for the next.

The plugin empties memory caches and outboxes before each test, and ``db``
puts the databases back, but a ``memory://`` ledger (the one tests use for
``LEDGERS``) and a ``MemoryStorage`` keep what earlier tests wrote, though
``storages.reset()`` exists for it. The databases' ids start again after
``db``, so a later test's ``customer:1`` meets the evidence an earlier
test recorded for its own ``customer:1``: a key written once is refused
(``LedgerKeyExists``), and reads return another test's facts. Only a test
whose application shuts down (a client's lifespan) closes the ledgers.
"""

import pytest

from domainkit_tests.test_testing import prefix, run_plugin

__all__ = ["prefix"]


def test_memory_ledgers_and_storages_start_empty_in_every_test(
    prefix: str, pytester: pytest.Pytester
) -> None:
    """What one test wrote to a memory ledger or storage is gone in the next."""
    pytester.makepyfile(
        test_kept="""
        import pytest

        from domainkit.conf import override_settings
        from domainkit.ledger import ledgers
        from domainkit.storage import storages

        def memory_backends():
            return override_settings(
                LEDGERS={"evidence": {"URL": "memory://"}},
                STORAGES={"default": {"BACKEND": "domainkit.storage.MemoryStorage"}},
            )

        pytestmark = pytest.mark.anyio

        async def test_records():
            with memory_backends():
                await ledgers["evidence"].create(b"customer:1", b"passport ok")
                await storages["default"].save(b"scan", directory="kyc")

        async def test_starts_empty():
            with memory_backends():
                found = await ledgers["evidence"].keys(b"", limit=10)
                assert found == []
                assert storages["default"].files == {}
        """
    )

    run_plugin(pytester).assert_outcomes(passed=2)
