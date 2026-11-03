# Part 0: install, and a first route

This part goes from `pip install` to one route and its test: a project
that answers, with nothing new to learn but the commands. It comes before
the five parts that build the bank ([the path](../tutorial.md)); each part
ends with a project that works.

You need Python 3.13 or 3.14, and `curl`.

## Install

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install "domainkit[postgres,redis,sqlite,testing]"
```

`sqlite` lets the project run without a database server; `postgres` is for
production, `redis` counts its rate limits and login attempts there, and
`testing` brings the pytest plugin.

## Create a project

```bash
domainkit init bankAPI bank
cd bank
python manage.py check
```

`bankAPI` is the project's name, and `bank` its directory. The name also
sets the prefix of its environment variables: `BANK_API_*`. On this
machine the project reads `config.settings.local` (`.env` says so), which
keeps its data in `db.sqlite3`. `check` reads the settings and the routes,
and says what is wrong before anything runs.

## A first route

A route is a function, as in FastAPI, which domainkit is built on. This one
answers the bank's interest rates. Make the file:

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

Every route says who may call it. `Public` means anyone; a route that says
nothing is refused by `check`, and production will not start with one. The
rates are strings, so `2.5` stays exactly `2.5` (a float would not).

Mount it with the project's other routes, at the end of `urls.py`:

```python title="src/config/urls.py"
from config.rates import router as rates_router

router.include_router(rates_router)
```

## Serve it and call it

```bash
python manage.py runserver
```

Leave it running. Open a second shell, `cd` to `bank`, activate the same
environment, and call it:

```bash
curl http://127.0.0.1:8000/rates
```

The answer is `{"savings":"2.5","checking":"0"}`. Stop the server with
Ctrl-C.

## A test

The `client` fixture calls the application in the test's own process, with
no server:

```python title="tests/test_rates.py (new file)"
"""Anyone may read the rates."""

from domainkit.testing import TestClient


def test_anyone_may_read_the_rates(client: TestClient) -> None:
    """No login is needed: the route is Public."""
    response = client.get("/rates")
    assert response.status_code == 200
    assert response.json() == {"savings": "2.5", "checking": "0"}
```

```bash
pytest
```

That is a domainkit project: settings, routes, and tests. Next,
[part 1](1-first-resource.md) adds the bank's accounts, as a domain: the
business's own part of the code, with its rules and its storage.
