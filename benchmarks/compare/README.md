# The same service on four frameworks

This compares this framework with Django, Litestar, and FastAPI under load,
the same way for each. The benchmark ladder (`python -m benchmarks`) counts
what one request costs inside this framework. This measures what users
see: requests a second and latency, against PostgreSQL.

## What every app serves

Every app serves the same three routes, from the same table and rows
(`schema.sql`: `compare_items`, 1,000 rows, remade before each app):

| Route | Answers |
| --- | --- |
| `GET /items/{id}` | 200 with `{"id", "name", "price"}`; 404 for an unknown id |
| `GET /items` | 200 with `{"items": [...]}`, the 20 newest |
| `POST /items` | 201 with the new item; 400 or 422 for a bad body (name 1 to 50 characters; price 0 or more, 10 digits, 2 after the point) |

Every route asks for `X-API-Key` and answers 401 without it, compared in
constant time. Each request makes one SQL statement, through a pool of 10
connections, on asyncpg or psycopg 3.

## The rules that keep it fair

- Each app is written as its framework's documentation teaches:
  - ours: an aggregate, a repository, and a unit of work;
  - FastAPI and Litestar: SQLAlchemy Core with Pydantic or msgspec;
  - Django: async views, a form, and the ORM.
- Each app is served by uvicorn, one process, with no access log, on the
  same machine and database.
- This framework keeps its default middleware (request ids, security
  headers, rate limits, deadlines) and its checks, because a project gets
  them. The others run with no middleware. The published table says so.
- Before each route is measured, the load runs for a warm-up period.
  Then `oha` sends the same load to every app: the same duration and the
  same number of connections.
- `--check` runs before any measurement. An app that answers anything
  wrong is not measured.

## Running it

On a machine that runs nothing else, with PostgreSQL 16 or later, and
[oha](https://github.com/hatoo/oha) on the `PATH`:

```bash
python3.13 -m venv .compare && . .compare/bin/activate
pip install -e ".[postgres]" -r benchmarks/compare/requirements.txt
export COMPARE_DATABASE_URL=postgresql+asyncpg://postgres@localhost:5432/postgres
python -m benchmarks.compare.run --check
python -m benchmarks.compare.run --duration 60s --connections 64 --json results.json
```

`--check` makes the table, starts each app, and checks the answers above.
Without it, the runner measures each route of each app and prints a
Markdown table: requests a second, median and 99th percentile latency,
and the answers that were not the expected status.

## Reading the numbers

With 64 connections, one process is saturated, so a request's latency is
mostly the time it waits for the requests ahead of it. The mean is then
about the connections over the requests a second (Little's law), and it
follows the cost of a request. The median does not: it depends on how
fairly each framework's event loop and pool serve the waiting requests.
A framework that lets most requests through quickly and makes a few wait
long has a low median and a high 90th percentile, at the same mean.

The first, indicative run measured a read at a median of 92 ms here and
15 ms on FastAPI. Measured again at three loads, one uvicorn process each,
against the same PostgreSQL:

| Connections | ours: mean | ours: p50 | ours: p90 | FastAPI: mean | FastAPI: p50 | FastAPI: p90 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1.71 ms | 1.63 ms | 2.09 ms | 1.14 ms | 1.06 ms | 1.45 ms |
| 10 | 13.4 ms | 13.2 ms | 16.8 ms | 8.3 ms | 8.2 ms | 9.9 ms |
| 64 | 78.7 ms | 73.0 ms | 136.8 ms | 52.3 ms | 12.0 ms | 164.7 ms |

So a read costs this framework about 0.6 ms more than FastAPI's
SQLAlchemy Core app, 1.5 times its time, not 6 times. Compare the means,
and run once with `--connections 1` for the cost of one request without
waiting. The ladder (`python -m benchmarks read --profile-dir DIR`) shows
where the time goes: about a quarter in the ORM's unit of work (the
session, loading, the identity map, events), and 1 to 4% in each layer of
the default middleware.

## Publishing the numbers

Publish them only with:
- the machine (CPU model, cores, memory) and the operating system;
- PostgreSQL's version, and whether it ran on the same machine;
- `pip freeze` of the environment, and the commit measured;
- the command line;
- three runs, each started fresh, with the median of the three.

A difference of less than 10% between two runs of the same app is noise
on most machines. Do not report it as a result.
