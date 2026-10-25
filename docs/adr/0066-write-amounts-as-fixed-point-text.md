# 0066. Write amounts as fixed-point text

- Status: Accepted
- Date: 2026-10-25

## Context

An amount (`Decimal`) leaves domainkit as text in many places: API
responses, CSV and NDJSON exports, outbox events and task arguments,
webhook payloads, audit details, evidence facts, cursors, fixtures. Most
of them used pydantic's JSON form, `str(value)`, which writes
`Decimal("1E+3")` as `"1E+3"` and `Decimal("0E-8")` as `"0E-8"`. Evidence
wrote `format(value, "f")`, `"1000"`, and cursors the same within 40
digits. The same amount therefore had two texts in two proofs (audit and
evidence), a reconciliation by string comparison failed, and a
spreadsheet read a CSV cell of `1E+3` as a float (audit 0.9.0, FIN-82).
Such values are not exotic: arithmetic gives them (`Decimal("10") *
Decimal("1E+2")`, a sum of zeros at eight places).

Options considered:

- **A `Money` type with quantized amounts.** The right end state (0.10,
  with currencies and rounding rules), but a new type every project must
  adopt; it fixes nothing for the `Decimal` fields that exist today.
- **One pydantic setting.** There is none for decimals; `json_encoders`
  is deprecated, and a `TypeAdapter` refuses a config for a dataclass
  (events are dataclasses).
- **Dump in Python mode and encode the result.** Serializers that apply
  only in JSON mode (`when_used="json"`), and a model's own JSON settings,
  would be lost: payloads could change in ways nobody asked for.
- **Walk the value and pydantic's JSON form of it together**, replacing
  only the text pydantic wrote for a `Decimal`.

## Decision

- `domainkit.utils.decimals.decimal_text(value)` is the one text form:
  fixed point, the value's own places (`"5.00"`), and no sign on zero
  (`"-0.00"` is `"0.00"`). Past 100 digits before or after the point, a
  value is no amount, and is written as `str()` writes it, so text stays
  bounded (`1E+999999999` would be a billion characters); NaN and the
  infinities too.
- Schemas from `schema_for` write their `Decimal` fields with it in JSON
  mode (a `PlainSerializer` with `when_used="json"`); Python mode still
  gives `Decimal`, and the JSON schema is unchanged. Exports are made from
  those documents, and a CSV cell given a `Decimal` uses it too.
- Outbox events (`dump_event`), task arguments, webhook payloads, audit
  details and audited events, privacy exports, the admin API's user
  fields, dynamic settings and `dumpdata` go through `with_decimal_text`:
  the value and its JSON form are walked together (fields of dataclasses
  and models, items of mappings, members of lists, tuples and sets), and a
  string is replaced only where the value held a `Decimal` and pydantic
  wrote `str(value)`. A serializer's own output, numbers, and strings that
  only look like amounts are left alone, so no path changes between JSON
  numbers and strings.
- Evidence and cursors call `decimal_text` where they formatted
  themselves; cursors keep their own 40-digit bound.
- A personal value's pseudonym hashes the same text. An audited event's
  personal field is hashed from its details, which now hold the fixed
  form, so `pseudonym_of` must hash it too, or it would no longer find
  an amount it was given as a `Decimal` with an exponent. Equal amounts
  now give one pseudonym whatever their form; a pseudonym stored before
  of an amount written with an exponent is found with that text
  (`pseudonym_of(subject, field, "1E+3")`).

Left as they are, on purpose:

- log lines (`default=str`): not records, and their bytes are kept equal
  to `json.dumps` (ADR 0063);
- cache values and keys: written and read only by domainkit, never
  compared as text by anyone else;
- localized formatting (`domainkit.i18n`), which is for people.

## Consequences

The bytes change only for amounts pydantic wrote with an exponent, and for
negative zero. An amount with a positive exponent reads back equal but with
exponent 0 (`Decimal("1000")` for `Decimal("1E+3")`); `==` and hashing do
not see the difference. A project's own response models keep pydantic's
form unless they use the serializer (`docs/databases.md`, "Exact amounts").

Writing an event or an audit row costs one more walk over its values, and
each amount in a schema's JSON one Python call. The `Money` type of 0.10
should carry quantized amounts and use the same text.
