# 0023. Keyset pages with versioned, typed cursors; filters declared per field

- Status: Accepted
- Date: 2026-10-19

## Context

Three copies of keyset paging existed (the generated domain, and auth's
users and groups), each with its own cursor of plain strings: no version,
no record of the ordering, and no types, so a cursor's values were parsed
by hand, or not at all. There was no limit/offset mode, no count, and no
way for clients to filter or order a listing except with bespoke query
parameters.

DRF offers `CursorPagination` (one ordering field, positions as text, an
invalid cursor is a 404) and `LimitOffsetPagination` (an unbounded offset);
django-filter offers `FilterSet`, with comma-separated lists.

## Decision

- `Repository.paginate(query, limit=, after=, offset=, count=)` builds on
  `filter`. The ordering always ends with the id, so a position (the last
  item's values of the ordering's fields) is unique. What follows a
  position is expressed as criteria in the domain, so it handles mixed
  directions and missing values, and fakes page as SQL does.
- A cursor is base64url JSON: `[1, "-placed_at,id", ...values]`, the
  format's version, the ordering it belongs to, then each value.
  Strings, integers, and booleans are JSON's; amounts, ids, and times are
  text; enums are their values and value objects what they wrap. Decoding
  checks the length, the alphabet, the version, the ordering, the number
  of values, and each value against its field's type hint; anything else
  is a 422 `invalid_cursor`, the code the old cursors had. Cursors are
  not signed: a client can only choose where a page starts, and the
  values are bound parameters. They are not encrypted either: they show
  the sort values of an item the client has already received.
- Offsets are a separate mode, bounded by `max_offset` (10,000). Counts
  are optional (`count=True`), as each costs a query.
- `FilterSet` allowlists lookups per field, the ordering fields, and the
  search fields. Its parameters are typed from the model's hints, so
  OpenAPI documents types and enums, and FastAPI checks them. Lists
  (`__in`, `ordering`) are repeated parameters, typed item by item, not
  comma-separated text. Counts of conditions, values, search words, and
  ordering fields, and text lengths, are bounded. What FastAPI cannot see
  is a 422 `invalid_filter`, whose message names only declared filters.

## Consequences

Generic listings (WP2's resource router) can page, count, filter, order,
and search any aggregate with these pieces. The auth admin's user and
group listings moved to them, so their cursors changed format: a cursor
issued before the upgrade is refused once with `invalid_cursor`, and the
client starts again. Changing the ordering also invalidates a cursor.
There is no previous-page cursor yet. Clients used to DRF's comma-separated
`?ordering=a,-b` must repeat the parameter instead. Endpoints must order
only by fields their clients may see, since cursors reveal sort values.
