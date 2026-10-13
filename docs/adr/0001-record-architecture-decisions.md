# 0001. Record architecture decisions

- Status: Accepted
- Date: 2026-10-14

## Context

domainkit is a framework: every decision in it is copied into each service
built on it, and the reasons behind those decisions are easily lost.

## Decision

Keep architecture decision records in `docs/adr/`, one short Markdown file
per decision, numbered in order and never rewritten once accepted.

## Consequences

Reviewers can ask for a record in a pull request that changes a decision.
Superseded records stay as history.
