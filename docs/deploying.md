# Deploying

This page takes a project whose `check` and tests pass to one that serves
in production. It is a checklist, in order: what `init` already gave you,
what to do before the first deployment, at the edge, in each rollout,
and once the service runs. Each item says what to do, and links the page
that says how.

On this page you learn how to:

- use the image and the CI workflow that `init` writes;
- lock the dependencies by hash, and pin the base image by digest;
- prepare the database, Redis, secrets, hosts, and proxies;
- roll out a release: the migration job first, then the processes;
- set up alerts, backups, and upgrades once the service runs.

[operations.md](operations.md) explains what runs and why.
[security.md](security.md) is the hardening checklist that startup and
`check --deploy` enforce.

## What `init` gives you

`domainkit init` writes three files for deployment.

### The image

The `Dockerfile` builds an image that serves `config.asgi:application`
with uvicorn, as a user without privileges who can write nothing in
`/app`:

```bash
docker build -t shop .
docker run --rm --env-file /etc/shop/production.env shop python manage.py migrate
docker run --rm -p 8000:8000 --env-file /etc/shop/production.env shop
```

It is built in two stages:

1. **The build stage** makes a virtual environment, `/opt/venv`, and
   installs the project's dependencies in it:
   - with a `requirements.lock`, the files the lock names, each checked
     against its hash (`pip install --require-hashes --no-deps -r
     requirements.lock`). A file whose hash does not match stops the
     build.
   - without a lock, the newest releases that `pyproject.toml` allows.
     The build prints `No requirements.lock: installing the newest
     releases pyproject.toml allows.`, and two builds of one commit may
     install different code.
2. **The final stage** copies the virtual environment and the code. The
   code is owned by root, and the process runs as uid 10001, so the
   application reads its code and cannot change it. The stage sets
   `DOMAINKIT_PROJECT_ROOT=/app` and
   `<PREFIX>SETTINGS_MODULE=config.settings.production`, and runs uvicorn
   with the limits of [Server limits](reference/servers.md).
   `WEB_CONCURRENCY` sets uvicorn's worker processes: one per core (one
   unless set).

`docker build --build-context wheels=<directory> .` offers the wheels in
`<directory>` (a fork, a release candidate) ahead of the package index,
for a build without a lock.

Every setting comes from the environment, as
`src/config/settings/production.py` lists them. Nothing secret is built
in. Keep the env file outside the project
(`/etc/<project>/production.env`), or better, in a secret store.

**`.dockerignore`** leaves every file out, and lets in only `manage.py`,
`pyproject.toml`, `requirements.lock`, `src/`, and `migrations/`. Even
inside those, it keeps out env files, `secrets/`, SQLite files, and
compiled Python. Add a `!` line for any other directory the application
reads, such as `LOCALE_PATHS` outside `src/`. **`.gitignore`** keeps env
files and `secrets/` out of the repository.

### The CI workflow

On every push and pull request, `.github/workflows/ci.yml` runs two
jobs:

| Job | Steps |
| --- | --- |
| Lint, check, and test | Install the dependencies (from `requirements.lock` by their hashes when there is one, as the image does; else those `pyproject.toml` names), then the `dev` group. Audit them against the advisories (`pip-audit`). `ruff check` and `ruff format --check`. `manage.py check`. `migrate` and `makemigrations --check` on a SQLite file. `pytest`. |
| Build the image | `docker build`. |

The commands run with the local settings (`config.settings.local`);
pytest uses `config.settings.test`. The workflow's actions are pinned to
commit SHAs: let Renovate or Dependabot update them. A project made with
`--profile fintech` also runs `mypy --strict`
([fintech.md](fintech.md)).

### The production settings

`config/settings/production.py` reads every address and secret from the
environment ([Configuration](operations.md#configuration)). Startup
fails, and names the variable, when one is missing.

## Before the first deployment

- [ ] **Dependencies locked.** Write the lock, with a hash for each file,
  and commit it. The image and CI then install exactly the files you
  reviewed:

  ```bash
  uv pip compile pyproject.toml --generate-hashes -o requirements.lock
  ```

  Pin the base image by digest too (`python:3.13-slim@sha256:...`), in
  both stages of the `Dockerfile`. Let Renovate or Dependabot keep the
  lock and the digest new.

- [ ] **Migrations committed.** `makemigrations` wrote one per model
  change. CI's `makemigrations --check` fails when one is missing.
- [ ] **The image builds in CI**, and is pushed to your registry by tag
  or digest, never `latest`.
- [ ] **PostgreSQL** with TLS that the client verifies (`SSLMODE =
  "verify-full"` is in the production settings), and two roles:
  - the application's role, which cannot change the schema, in
    `DATABASE_URL`;
  - the owner's role, in `DATABASES["default"]["MIGRATION_URL"]`
    ([Migrations in a rollout](operations.md#migrations-in-a-rollout)).
- [ ] **Redis** over `rediss://` with a password, in `REDIS_URL`. It
  counts requests and login attempts once for every process.
- [ ] **Secrets** from a secret store, as files where it mounts them
  (`<NAME>_FILE=/run/secrets/<name>`): `SECRET_KEY` (50 random characters
  or more), the database and Redis URLs, and the JWT key if you use one
  ([security.md](security.md), "Settings and secrets"). An env file for
  `docker run --env-file` lives outside the project
  (`/etc/<project>/production.env`), never next to the `Dockerfile`.
- [ ] **`ALLOWED_HOSTS`** names the hosts callers use, and
  **`TRUSTED_PROXIES`** the networks of your load balancers (empty only
  when callers connect directly).
- [ ] **Pools sized**: `POOL_SIZE` and `MAX_OVERFLOW` so that processes ×
  replicas × (`POOL_SIZE` + `MAX_OVERFLOW`) stays below the server's
  `max_connections` ([Sizing](operations.md#sizing)).
- [ ] **`check --deploy` in the pipeline**, with the production settings
  and the real variable names. It fails on every error, and on each
  warning you have not silenced (`SILENCED_CHECKS`).

## The edge

- [ ] **TLS ends at the proxy or load balancer**, which redirects plain
  HTTP to HTTPS. The application serves plain HTTP, and cannot see the
  TLS in front of it. `check` says so (`security.I001`), and says where
  it takes each request's scheme from.
- [ ] **The proxy sets `X-Forwarded-For`, `X-Forwarded-Proto`, and
  `X-Request-ID`** over what a caller sent
  ([Request ids](operations.md#request-ids)).
- [ ] **The proxy limits** header sizes and slow clients
  ([Server limits](reference/servers.md)).
- [ ] **Probes send a `Host` that `ALLOWED_HOSTS` lists.** Use `/health`
  for liveness and `/ready` for readiness. A request with any other
  host, a probe's included, is answered 400
  ([Health checks](operations.md#health-checks)). On Kubernetes:

  ```yaml
  livenessProbe:
    httpGet:
      path: /health
      port: 8000
      httpHeaders: [{name: Host, value: api.example.com}]
  ```

## Each rollout

- [ ] **The migration job first**, from the new image, while the old
  code serves: `python manage.py migrate --plan` to review, then
  `python manage.py migrate`. If the database cannot be reached, the
  command says so on one line and exits with status 1: retry the job.
- [ ] **Then the web processes and the workers**, from the same image:
  `python manage.py runworker` for the outbox, at least one per outbox
  database ([The worker](operations.md#the-worker)).
- [ ] **The grace period** of the orchestrator is above uvicorn's
  graceful timeout (10 seconds in the image) plus `SHUTDOWN_TIMEOUT`
  (15), and 45 seconds for workers
  ([Running the web processes](operations.md#running-the-web-processes)).
- [ ] **Roll forward, not back.** To undo a schema change, write a new
  revision. A downgrade in staging and production needs
  `--allow-data-loss`, and is recorded in the audit trail before it runs
  ([Rolling back a migration](operations.md#rolling-back-a-migration)).

## Once it serves

- [ ] **Alerts** on what [runbook.md](runbook.md) lists: shed requests,
  the outbox backing up, dead letters, open circuit breakers, and logins
  refused while Redis is down
  ([Metrics to alert on](operations.md#metrics-to-alert-on)).
- [ ] **Backups** of every database, restored once to prove they work
  ([Backups and restores](operations.md#backups-and-restores)).
- [ ] **Operators named.** People who run `manage.py` in production pass
  `--operator`, or set `DOMAINKIT_OPERATOR`
  ([Commands in production](operations.md#commands-in-production)).
- [ ] **Upgrades** to each 1.x minor while it is the latest, as
  [upgrading.md](upgrading.md) says, with `check --deploy` after each.

## Common mistakes

- **No `requirements.lock`.** Each build then installs the newest
  releases, so the image you tested is not the image you ship. Commit
  the lock.
- **A base image by tag only.** `python:3.13-slim` moves. Pin it by
  digest, and let a bot move the digest in a reviewed pull request.
- **An env file next to the `Dockerfile`.** `.dockerignore` keeps it out
  of the image, but it is still one `git add` from the repository. Keep
  it outside the project.
- **Probes without a `Host` header.** The orchestrator sends the pod's
  address, which `ALLOWED_HOSTS` does not list, and every probe gets
  400.
- **Migrations in the web process's start.** Run them once, as a job,
  before the new code serves.

## See also

- [Operations](operations.md): configuration, processes, migrations, and
  observability.
- [Security](security.md): the hardening checklist.
- [Server limits](reference/servers.md): the server's command and
  limits.
- [Deployment checks](reference/checks.md): every check, refused or
  warned.
- [Upgrading](upgrading.md).
