# Timing newcomers

How to measure the learning curve with people instead of guessing it: a
few Python developers new to the framework work through the first parts
of the [tutorial](tutorial.md), and an observer notes how long each part
takes and where they get stuck. Run it before and after a change meant to
make learning easier, and keep the results beside the change.

## Who takes part

Five people are enough to find most of what blocks newcomers. Each:

- writes Python at work, and has built a web API before;
- has not used this framework;
- is, across the five, a mix: some who know Django, some FastAPI, at least
  one who knows neither.

## The tasks

Each person works alone, in a fresh environment with Python 3.13 or 3.14,
from the published pages, in this order:

1. [Part 0](tutorial/0-first-route.md): install, a project, a first route
   and its test.
2. [Part 1](tutorial/1-first-resource.md): a domain, a user, requests
   behind a login.
3. [Part 2](tutorial/2-rules.md): an aggregate's rules and a migration.

They may use any other page of the documentation and a search engine,
and not an AI assistant (it would measure the assistant).

## The observer's rules

- Say only: "Think aloud, please; I won't help unless you are stuck for 10
  minutes."
- Start a clock at each part's first command; stop it when the part's last
  check passes (the part's `curl` answers, or `pytest` passes).
- After 10 minutes on one problem, give the smallest hint that unblocks,
  and note it: a hint is a finding.
- Note every place the person stops, rereads, leaves the page, or does
  something the page did not say. Do not record names, screens, or
  voices.

## What to record

One row per person and part, in a CSV file:

```text
person,background,part,minutes,finished,hints,stuck_at,left_the_page_for,error_messages_read
P1,django,0,9,yes,0,,,
P1,django,1,23,yes,1,"createsuperuser prompt",auth.md,"401 not_authenticated"
```

- `person`: P1 to P5, never a name.
- `background`: `django`, `fastapi`, `both`, or `neither`.
- `minutes`: whole minutes from the part's first command to its last check.
- `finished`: `yes`, or `no` after 45 minutes.
- `hints`: how many hints were given.
- `stuck_at`: the step or message where the person stopped longest.
- `left_the_page_for`: other pages or searches they went to.
- `error_messages_read`: errors they hit, as the framework printed them.

Then three questions to each person, answers kept as written:

1. What was the hardest moment, and why?
2. Which word or idea did you not understand when you first met it?
3. What would you have looked for first, had you been alone?

## Turning it into work

- A part whose median time is over 15 minutes (the tutorial's promise) is
  a finding.
- A step where two or more people got stuck is a finding.
- An error message someone read and could not act on is a finding:
  it should name the fix ([L1 in the roadmap](roadmap.md)).
- Each finding becomes a roadmap row or an issue, with the CSV rows that
  show it. Run the study again once they are fixed, with five other
  people.

## The first run: five agents, November 2026

Before any person took part, five AI agents worked through parts 0 to 2
as the protocol says, each in the role of a background (two Django, two
FastAPI, one neither), from the published 1.6.0 package and these pages
only. An agent is not a newcomer: it reads fast and is never tired, so
its minutes say nothing about a person's, and the rule against an AI
assistant above stands for the study with people. What a run of agents
can show is where the pages are wrong or unclear, and each stop it made
is a finding all the same.

```text
person,background,part,seconds,commands,finished,hints
P1,django,0,54,11,yes,0
P1,django,1,28,15,yes,0
P1,django,2,46,13,yes,0
P2,django,0,57,13,yes,0
P2,django,1,35,17,yes,0
P2,django,2,45,18,yes,0
P3,fastapi,0,59,14,yes,0
P3,fastapi,1,30,16,yes,0
P3,fastapi,2,42,18,yes,0
P4,fastapi,0,50,12,yes,0
P4,fastapi,1,28,15,yes,0
P4,fastapi,2,42,18,yes,0
P5,neither,0,51,13,yes,0
P5,neither,1,30,16,yes,0
P5,neither,2,39,19,yes,0
```

Every command on the pages ran as written. What made them stop, with how
many of the five, and what changed:

| Finding | Of five | Fixed by |
| --- | --- | --- |
| Every generator warned that ruff is not installed; the warning did not say how, and the new project then failed its own lint | 5 | `pip install --group dev` in part 0, the README, and `init`'s next steps; the warning names the command |
| `init`'s next steps (an `auth` migration, a user) differ from part 0's | 5 | Part 0 says the tutorial takes them in its own order |
| "Add the `Digits` import" when the file already imports from `domainkit.ddd` | 5 | Part 2 says where it goes |
| The first migration, "create accounts", also makes the users', tokens', and messages' tables | 5 | Part 1 says so |
| "Port" and "bounded context" not in the glossary; its comparisons all Django | 4 | Both words added, with Flask, FastAPI, and SQLAlchemy beside Django |
| No page says how to serve on another port | 3 | Part 0 does |
| No way back to `$TOKEN` and `$ID` in a new shell | 3 | Part 2 links to part 1's lines |
| No page for someone who knows FastAPI | 2 | [from-fastapi.md](from-fastapi.md) |
| [from-django.md](from-django.md) not linked from the tutorial | 2 | Linked from the path |
| Whether to replace the migration's `upgrade()` | 2 | Part 2 says so |
| The code of the `1.005` refusal not shown | 2 | Part 2 shows it |
| `. .venv/bin/activate` fails from `bank/` | 1 | Part 0 gives the path |
| CI's image is named `bankAPI`, which Docker refuses | 1 | The image is named in lower case |

The study with five people is still to run, on these pages.
