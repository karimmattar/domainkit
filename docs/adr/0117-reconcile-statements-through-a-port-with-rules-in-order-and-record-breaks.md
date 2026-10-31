# 0117. Reconcile statements through a port, match by rules in order, and record breaks

- Status: Accepted
- Date: 2026-10-30

## Context

The fintech audit's FIN-11: nothing compared the books with what a bank
or a payment service says happened. Safeguarding rules for e-money and
payment institutions, and any treasury, ask for that comparison every
day, and the differences it finds (breaks) are a control of their own.
The audit proposed an app of its own; the matching needs the postings,
their entries' references, and the accounts' locks, so it lives in
`domainkit.contrib.accounting` (ADR 0116), in its own module. The
questions were how statements come in, how lines and postings are
paired, and what is kept.

**Coming in.** Statements arrive as files (a bank's CSV, camt.053,
MT940, BAI2) or from an API. A port, `StatementSource`, gives the lines
(`StatementLine`: an id on the statement, a date, a `Money`, a
reference, a description); `CsvStatement` is the one adapter shipped,
with the columns renamed per bank. Amounts are read as text, digits and
a point only, and refused past the currency's places: a statement's
`1e3` or `1 000.00` is an error, never a float. A line is stored once per
account by its id (`ON CONFLICT DO NOTHING` on `(account_id,
external_id)`), so a file imported twice imports nothing more, and every
line is checked before any is stored.

**Pairing.** A statement's sign is the account's: positive is a debit
(money into a bank account kept as an asset). Banks pair by reference
when one is there, and otherwise by amount within a few days, but only
where the amount says which: two payments of 25.00 on two days paired
by guess are worse than two breaks, since a wrong match hides a missing
payment. A rule is a class with a `name` and `match(lines, postings,
window)` returning pairs, each line and posting in one pair at most;
`ACCOUNTING.MATCHING_RULES` lists them, tried in order, each on what the
ones before left. The two shipped are `ByReference` (same reference,
amount, and currency, the nearest date within the window) and
`ByAmountAndDate` (same amount and currency within the window, only
where each is the other's one candidate). A project adds its own (a
PSP's settlement ids, a rule of tolerance) by path. A rule that pairs a
line or a posting twice is a bug in it, and nothing is recorded.

**What is kept.** Each match is a row naming the line, the posting, and
the rule; a posting is matched once. What is left after the rules is
recorded as breaks: lines without a posting, and postings without a
line dated within the days the account's statements cover (a posting
after the last statement is not a break yet). Each run replaces the
account's breaks, so `breaks_of` (and `manage.py accounting unmatched`,
and `GET /accounts/{id}/breaks`) lists what is open now. A run holds a
lock of the account's name, so two runs never record the same match.

## Decision

- `domainkit.contrib.accounting.reconciliation`: the port
  `StatementSource`, the adapter `CsvStatement`, `import_statement`,
  `reconcile`, `breaks_of`, and the protocol `MatchingRule` with the two
  rules shipped, listed in `ACCOUNTING.MATCHING_RULES`.
- Lines are imported once by their id, amounts exact; matches and breaks
  are rows; each reconciliation replaces the account's breaks.
- Importing and reconciling need `accounting.reconcile_account`, over
  HTTP and as recorded operator actions of `manage.py accounting`.

## Consequences

- A rule pairs one line with one posting. A deposit that settles three
  sales, or a fee netted from a payout, stays a break until a project's
  rule (or a person) splits it; one-to-many matching is left open, and
  so are manual matches under maker-checker and sign-off recorded as
  evidence.
- Statement descriptions are kept as the bank wrote them and may name
  people; erasure does not reach them, as they are not linked to a
  subject. Import only what reconciliation needs.
- Formats other than CSV are adapters a project writes against the port
  until the parsers of FIN-12 arrive.
