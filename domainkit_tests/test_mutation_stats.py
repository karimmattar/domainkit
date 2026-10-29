"""mutmut's stats run sees every test that calls a function behind a cache."""

import pytest

from domainkit.contrib.auth import repositories
from domainkit.http import ratelimit
from domainkit_tests.conftest import domainkit_caches, forget_caches_under_mutmuts_stats


def test_the_caches_of_every_loaded_module_are_found() -> None:
    """Functions behind ``functools.cache`` and ``lru_cache``, each once."""
    found = domainkit_caches()

    assert repositories._principal_statement in found
    assert ratelimit._cached_address_group in found
    assert len(found) == len({id(each) for each in found})


def test_only_the_stats_run_forgets_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutant's run, and any other, keeps what its tests cached."""
    ratelimit._cached_address_group("203.0.113.1")
    monkeypatch.setenv("MUTANT_UNDER_TEST", "domainkit.x__mutmut_1")

    assert forget_caches_under_mutmuts_stats() == 0
    assert ratelimit._cached_address_group.cache_info().currsize >= 1

    monkeypatch.setenv("MUTANT_UNDER_TEST", "stats")
    assert forget_caches_under_mutmuts_stats() == len(domainkit_caches())
    assert ratelimit._cached_address_group.cache_info().currsize == 0
