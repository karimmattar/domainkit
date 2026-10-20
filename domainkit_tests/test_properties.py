"""Property-based tests: invariants that must hold for any input."""

import asyncio
import ipaddress
import re
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit.conf.env import _FALSE as FALSE
from domainkit.conf.env import _TRUE as TRUE
from domainkit.conf.env import Env, prefix_for
from domainkit.db import UTCDateTime
from domainkit.ddd import ValueObject
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import (
    MAX_CURSOR_LENGTH,
    InvalidCursor,
    decode_cursor,
    encode_cursor,
)
from domainkit.http.middleware import host_allowed
from domainkit.http.ratelimit import Decision, MemoryBackend
from domainkit.utils.rates import PERIODS, parse_rate
from domainkit.utils.text import camel_case, snake_case

IDENTIFIER = re.compile(r"[a-z0-9_]*")
SQLITE = create_engine("sqlite://").dialect
POSTGRES = create_async_engine("postgresql+asyncpg://app@db/app").dialect


class Point(ValueObject):
    """A value object with two fields."""

    x: int
    y: int


@given(st.text(alphabet=st.characters(categories=("Lu", "Ll", "Nd")), max_size=40))
def test_snake_case_yields_identifiers(name: str) -> None:
    """ASCII CamelCase names become lowercase snake case, stably."""
    ascii_name = name.encode("ascii", "ignore").decode()
    result = snake_case(ascii_name)

    assert IDENTIFIER.fullmatch(result)
    assert snake_case(result) == result
    assert "_" not in camel_case(result)


@given(st.text(max_size=60))
def test_prefixes_are_valid_variable_names(name: str) -> None:
    """Any project name gives an UPPER_SNAKE prefix ending in "_" (or none)."""
    prefix = prefix_for(name)

    assert prefix == "" or re.fullmatch(r"[A-Z0-9_]*[A-Z0-9]_", prefix)


@given(st.lists(st.text(max_size=30), min_size=1, max_size=4))
def test_cursors_round_trip(values: list[str]) -> None:
    """Any list of strings survives encoding, whatever its characters."""
    cursor = encode_cursor(values)

    assert re.fullmatch(r"[A-Za-z0-9_-]*", cursor)
    assert decode_cursor(cursor, len(values)) == values


@given(st.text(max_size=3000), st.integers(min_value=1, max_value=4))
def test_forged_cursors_never_crash(cursor: str, size: int) -> None:
    """Decoding arbitrary text either works or raises InvalidCursor."""
    try:
        values = decode_cursor(cursor, size)
    except InvalidCursor:
        return
    assert len(values) == size


@given(st.text(max_size=300), st.lists(st.text(max_size=40), max_size=4))
def test_host_matching_never_crashes(host: str, allowed: list[str]) -> None:
    """Untrusted Host headers are only ever accepted or refused."""
    assert host_allowed(host, allowed) in {True, False}


@given(st.ip_addresses())
def test_exact_hosts_match_themselves(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> None:
    """An allowed IP host (bracketed for IPv6) is accepted, with or without port."""
    host = f"[{address}]" if address.version == 6 else str(address)

    assert host_allowed(host, [host])
    assert host_allowed(f"{host}:8443", [host])


@given(
    st.integers(min_value=1, max_value=10_000),
    st.sampled_from(sorted(PERIODS)),
)
def test_rates_round_trip(count: int, period: str) -> None:
    """Every well-formed rate parses to its count and period."""
    assert parse_rate(f"{count}/{period}") == (count, PERIODS[period])


@given(st.text(max_size=40))
def test_malformed_rates_raise_value_error(text: str) -> None:
    """Parsing never fails with anything but ValueError."""
    try:
        parse_rate(text)
    except ValueError:
        return


@settings(max_examples=60)
@given(
    limit=st.integers(min_value=1, max_value=20),
    period=st.integers(min_value=1, max_value=60),
    gaps=st.lists(st.floats(min_value=0, max_value=5), min_size=1, max_size=120),
)
def test_rate_limits_are_never_exceeded(
    limit: int, period: int, gaps: list[float]
) -> None:
    """Never more than the burst plus the refill between any two moments."""
    clock = [0.0]
    backend = MemoryBackend(clock=lambda: clock[0])
    allowed_at = []
    for gap in gaps:
        clock[0] += gap
        if asyncio.run(backend.hit("caller", limit, period)).allowed:
            allowed_at.append(clock[0])

    # Token-bucket guarantee: between any two allowed requests, the count
    # never exceeds the burst plus what refilled in between.
    interval = period / limit
    for first in range(len(allowed_at)):
        for last in range(first, len(allowed_at)):
            elapsed = allowed_at[last] - allowed_at[first]
            assert last - first + 1 <= limit + elapsed / interval + 1e-6


@given(
    start=st.floats(min_value=0, max_value=1e7),
    limit=st.integers(min_value=1, max_value=20),
    period=st.integers(min_value=1, max_value=3600),
    hits=st.integers(min_value=1, max_value=25),
)
def test_rate_limits_admit_new_callers_at_any_clock_reading(
    start: float, limit: int, period: int, hits: int
) -> None:
    """A caller's first request passes; every refusal says how long to wait.

    The clock starts anywhere: ``time.monotonic()`` counts from boot, and
    rounding depends on its magnitude.
    """
    backend = MemoryBackend(clock=lambda: start)

    async def decide() -> list[Decision]:
        return [await backend.hit("caller", limit, period) for _ in range(hits)]

    decisions = asyncio.run(decide())
    assert decisions[0].allowed
    assert all(d.retry_after > 0 for d in decisions if not d.allowed)


@given(st.text(max_size=20))
def test_boolean_variables_parse_or_explain(text: str) -> None:
    """A boolean variable is true, false, or a configuration error."""
    env = Env(prefix="P_", env_file=None)
    env.__dict__["file_values"] = {"P_FLAG": text}
    normalized = text.strip().lower()
    if normalized in TRUE | FALSE:
        assert env.bool("FLAG") is (normalized in TRUE)
    else:
        with pytest.raises(ImproperlyConfigured, match="P_FLAG must be a boolean"):
            env.bool("FLAG")


@given(
    st.datetimes(
        min_value=datetime(1970, 1, 2),  # noqa: DTZ001 - bounds for aware values
        max_value=datetime(9999, 12, 30),  # noqa: DTZ001
        timezones=st.timezones(),
    )
)
def test_timestamps_round_trip_in_utc(moment: datetime) -> None:
    """Aware datetimes are stored and read back as the same instant, in UTC."""
    column = UTCDateTime()
    for dialect in (SQLITE, POSTGRES):
        stored = column.process_bind_param(moment, dialect)
        loaded = column.process_result_value(stored, dialect)
        # Compare instants: zones disagree on equality inside DST folds.
        assert loaded == moment.astimezone(UTC)
        assert loaded is not None
        assert loaded.utcoffset() == timedelta(0)
        assert loaded.tzinfo is UTC


@given(st.integers(), st.integers(), st.integers(), st.integers())
def test_equal_values_hash_equally(a: int, b: int, c: int, d: int) -> None:
    """Value objects are equal exactly when their fields are, and hash alike."""
    first, second = Point(a, b), Point(c, d)

    assert (first == second) is ((a, b) == (c, d))
    if first == second:
        assert hash(first) == hash(second)


def test_the_longest_names_fit_in_a_cursor() -> None:
    """A 200-character name in a 4-byte script plus an id still round-trips."""
    values = ["\U0001f600" * 200, "f0e1d2c3-b4a5-4697-8879-6a5b4c3d2e1f"]

    assert decode_cursor(encode_cursor(values), 2) == values
    with pytest.raises(ValueError, match="too long"):
        encode_cursor(["x" * MAX_CURSOR_LENGTH])
