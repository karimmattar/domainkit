# 0023. Keyset pages with versioned, typed cursors; filters declared per field

- Status: Accepted
- Date: 2026-10-19; amended 2026-10-20 (bounded cursors)

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

Amendment: cursors were refused on the way out when longer than the
2,048 characters accepted on the way in. One long sort value (a title of
800 Arabic letters, 1,600 bytes) then made every page ending on it a 500.

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
- Cursors that come in are at most `MAX_CURSOR_LENGTH` (4,096)
  characters, so decoding untrusted input stays cheap. Cursors that go
  out are never refused, so a page always answers. What an ordering can
  put in a cursor is bounded instead, from the fields' declarations:
  `cursor_length(ordering, model)` adds up each field's longest JSON
  (text needs `MaxLength`, and counts 6 bytes a character, as JSON
  writes `\u001f`; amounts need `Digits`; enums their longest value).
  A `FilterSet` refuses, when declared, an ordering field without such a
  bound, and a default ordering, or the longest one clients may ask for
  (its `max_ordering` roomiest `ordering_fields`, descending), whose
  cursor could be longer than the cap. A stored value longer than its
  field declares still gets a cursor, which is logged and refused when
  it comes back. Raising the cap alone would bound nothing, since text
  without `MaxLength` has no limit, and URLs past about 8 KB are refused
  by common proxies; it rose from 2,048 so that three text fields of
  `MaxLength(150)` fit. (Amended 2026-10-20.)
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

Since the amendment, ordering by text needs `MaxLength` on the field
(generated filter sets must leave unbounded text out of `ordering_fields`
or declare one), and three text fields of `MaxLength(150)` fit the cap
together with a UUID id; longer ones need a lower `max_ordering`.
Amounts are written in plain notation in cursors (`0.00000000`, not
`0E-8`, which decoding refused).
