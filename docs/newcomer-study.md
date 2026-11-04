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
