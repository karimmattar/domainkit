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

## Publishing the numbers

Publish them only with:
- the machine (CPU model, cores, memory) and the operating system;
- PostgreSQL's version, and whether it ran on the same machine;
- `pip freeze` of the environment, and the commit measured;
- the command line;
- three runs, each started fresh, with the median of the three.

A difference of less than 10% between two runs of the same app is noise
on most machines. Do not report it as a result.
