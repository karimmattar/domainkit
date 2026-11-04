# Getting started

This tutorial teaches domainkit by building one thing: a small bank API.
It has six short parts. You start with `pip install`, and you end with a
tested service that has accounts, rules that guard their balance, users
who may do only what they were granted, and a reaction that runs in a
background worker. Each part takes about 15 minutes, and each one ends
with a project that works.

## What you will build

By the end, your project has:

- one public route, `GET /rates`, and its test;
- an `accounts` [domain](learning.md#domain) with an `Account`
  [aggregate](learning.md#aggregate): a name, a balance in exact money,
  and a flag that closes the account;
- deposits that the account refuses when they break its rules;
- a clerk who may read accounts, but not change them;
- a [durable handler](learning.md#handler) that the worker runs after
  each deposit;
- tests of the rules and of the API, and settings that pass the
  production checks.

## The parts

0. [Install, and a first route](tutorial/0-first-route.md): create a
   project, add one route, call it with `curl`, and test it. No new words
   to learn.
1. [A first resource](tutorial/1-first-resource.md): add a domain and a
   superuser, log in, and create and list accounts.
2. [An aggregate with rules](tutorial/2-rules.md): add a balance and a
   deposit, write their rules by hand, and migrate the table.
3. [Users and permissions](tutorial/3-permissions.md): make a clerk who
   may read accounts and do nothing more.
4. [Events and the outbox](tutorial/4-events.md): react to each deposit
   in a worker, with the reaction stored in the same transaction.
5. [Tests, and getting ready for production](tutorial/5-testing-and-deploying.md):
   test the rules, and run the checks that production runs at startup.

## Before you start

You need Python 3.13 or 3.14, and `curl`. You do not need a database
server: the tutorial keeps its data in a SQLite file.

Work through the parts in order, in one directory. Each part starts
where the one before ended. You need two shells: one runs the server,
and the other sends requests to it.

[The glossary](learning.md#words) explains each domainkit word the parts
use. If you know Django or FastAPI already,
[coming from Django](from-django.md) and
[coming from FastAPI](from-fastapi.md) map what you know to its name in
domainkit.

!!! note
    The tests run every command and every code change on these pages, as
    written and in order (`domainkit_tests/test_walkthroughs.py`). If a
    step fails for you, that is a bug. Please report it.

## After the tutorial

[Learning domainkit](learning.md) gives an order in which to read the
guides, the glossary, and the commands of a working day. Then:

- [Deploying](deploying.md): from a project whose checks pass to one in
  production, with the image and the CI workflow that `init` writes.
- [Running domainkit in production](operations.md): what runs, and how to
  watch it. The [runbook](runbook.md) says what to do when an alarm
  fires.
- [Hardening a deployment](security.md): the checklist to go through
  before real users arrive.
- [Upgrading](upgrading.md): how to move a project to a new version.
- [The public API](api.md): every public name. On the documentation
  site, "API reference" shows the docstring of each one.
