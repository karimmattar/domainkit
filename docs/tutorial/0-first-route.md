# Part 0: install, and a first route

In this part you install domainkit, create a project, and add one route
that anyone may call. You call the route with `curl`, and you write a
test for it. You learn no new words here, only the commands. This part
comes before the five parts that build a small bank
([the tutorial](../tutorial.md)). Each part ends with a project that
works, and the next part starts from it.

## What you will build

- A project called `bankAPI`, in a directory called `bank`.
- One route, `GET /rates`, that answers the bank's interest rates.
- One test that calls the route and passes.

You need Python 3.13 or 3.14, and `curl`. The part takes about 15
minutes.

## Step 1: install domainkit

Make a virtual environment, turn it on, and install domainkit with four
extras:

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install "domainkit[postgres,redis,sqlite,testing]"
```

Each extra adds an optional part:

- `sqlite` keeps the data in a file, so the project runs without a
  database server.
- `postgres` is the PostgreSQL driver, for production.
- `redis` lets production count rate limits and failed logins in Redis,
  once for all processes.
- `testing` adds domainkit's pytest plugin.

## Step 2: create a project

```bash
domainkit init bankAPI bank
cd bank
pip install --group dev
python manage.py check
```

`domainkit init bankAPI bank` creates the project. `bankAPI` is the
project's name, and `bank` is its directory. The name also sets the
prefix of the project's environment variables: `BANK_API_`. For example,
`BANK_API_SETTINGS_MODULE` names the settings module to use.

`pip install --group dev` installs the project's development tools. One
of them is ruff. ruff formats the code that the generators write in the
next parts.

On your machine, the project reads the settings module
`config.settings.local`, because the file `.env` says so. These settings
keep the data in the file `db.sqlite3`.

`python manage.py check` reads the settings and the routes. It reports
what is wrong before anything runs. On the new project it prints
`System check identified no issues. Domains: messaging, contenttypes, auth.`
Every new project has these three domains: `messaging` runs work after a
request, `contenttypes` lists the kinds of data that permissions refer
to, and `auth` holds users and their permissions.

`init` also prints a list of next steps. This tutorial takes them in its
own order: the database and the first user come in
[part 1](1-first-resource.md).

## Step 3: add a first route

A route is a function that answers requests to one URL. FastAPI, which
domainkit is built on, calls it a path operation. Flask and Django call
it a view. Your first route answers the bank's interest rates. Make this
new file:

```python title="src/config/rates.py (new file)"
"""The bank's interest rates: a first route."""

from domainkit.http.access import Public
from fastapi import APIRouter

router = APIRouter()


@router.get("/rates", dependencies=[Public])
async def rates() -> dict[str, str]:
    """Each kind of account's yearly rate, in percent."""
    return {"savings": "2.5", "checking": "0"}
```

Every route says who may call it. `dependencies=[Public]` means anyone
may call this one. A route that says nothing is never public by accident:
`check` lists it, and staging and production refuse to start with it.

The rates are strings, not floats. The string `"2.5"` stays exactly 2.5,
and a float cannot hold most decimal fractions exactly.

Now mount the new router beside the project's other routes. Add these
lines at the end of `src/config/urls.py`:

```python title="src/config/urls.py"
from config.rates import router as rates_router

router.include_router(rates_router)
```

## Step 4: serve it and call it

Start the development server:

```bash
python manage.py runserver
```

The server listens at `http://127.0.0.1:8000`, and it restarts when you
change the code. Leave it running. Open a second shell, go to the `bank`
directory, and turn on the same environment there
(`. ../.venv/bin/activate`). Then call the route:

```bash
curl http://127.0.0.1:8000/rates
```

The answer is `{"savings":"2.5","checking":"0"}`.

Stop the server with Ctrl-C. If port 8000 is taken on your machine,
`python manage.py runserver 8001` serves on port 8001. Then call that
port instead, here and in the next parts.

## Step 5: write a test

The `client` fixture comes with domainkit's pytest plugin. It calls the
application inside the test's own process, so no server needs to run.
Make this new file:

```python title="tests/test_rates.py (new file)"
"""Anyone may read the rates."""

from domainkit.testing import TestClient


def test_anyone_may_read_the_rates(client: TestClient) -> None:
    """No login is needed: the route is Public."""
    response = client.get("/rates")
    assert response.status_code == 200
    assert response.json() == {"savings": "2.5", "checking": "0"}
```

Run the tests:

```bash
pytest
```

pytest runs your test and the tests that `init` wrote, and prints
`4 passed`.

## What you learned

- `domainkit init` creates a project: settings, routes, `manage.py`, and
  tests. The project's name sets the prefix of its environment variables.
- `python manage.py check` finds problems in the settings and the routes
  before anything runs.
- A route is a FastAPI function on an `APIRouter`. You mount its router in
  `src/config/urls.py`.
- Every route says who may call it. `Public` means anyone.
- `python manage.py runserver` serves the project while you work on it.
- The `client` fixture calls the application in a test, with no server.

That is a whole domainkit project: settings, routes, and tests. Next,
[part 1](1-first-resource.md) adds the bank's accounts as a domain: the
part of the code that holds the business's own rules and data.
