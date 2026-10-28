# Deployment checklist

From a project whose `check` and tests pass to one that serves in
production, in order. Each item names what to do and the page that says
how. [operations.md](operations.md) explains what runs and why;
[security.md](security.md) is the hardening checklist that startup and
`check --deploy` enforce.

## What `init` gives you

- **`Dockerfile`**: the image. Dependencies from `pyproject.toml` in a
  build stage; the code copied as root and run as uid 10001, who can
  write nothing in `/app`; `<PREFIX>SETTINGS_MODULE` set to
  `config.settings.production`; uvicorn with the limits of
  [Server limits](reference.md#server-limits). `WEB_CONCURRENCY` sets
  its worker processes, one per core. `.dockerignore` lets in only
  `manage.py`, `pyproject.toml`, `src/`, and `migrations/`, and keeps env
  files, `secrets/`, and SQLite files out even there: add a `!` line for
  any other directory the application reads. `.gitignore` keeps env files
  and `secrets/` out of the repository.
- **`.github/workflows/ci.yml`**: on every push and pull request, ruff,
  `check`, `migrate` and `makemigrations --check` on a SQLite file, the
  tests, and a build of the image. Its actions are pinned to commit
  SHAs; let Renovate or Dependabot update them.
- **`config/settings/production.py`**: every address and secret from the
  environment ([Configuration](operations.md#configuration)).

## Before the first deployment

- [ ] **Migrations committed.** `makemigrations` wrote one per model
      change; CI's `makemigrations --check` fails when one is missing.
- [ ] **The image builds in CI**, and is pushed to your registry by tag
      or digest, never `latest`.
- [ ] **PostgreSQL** with TLS the client verifies (`SSLMODE =
      "verify-full"` is in the production settings), and two roles: the
      application's, which cannot change the schema, in `DATABASE_URL`,
      and the owner's in `DATABASES["default"]["MIGRATION_URL"]`
      ([Migrations in a rollout](operations.md#migrations-in-a-rollout)).
- [ ] **Redis** over `rediss://` with a password, in `REDIS_URL`: it
      counts requests and login attempts once for every process.
- [ ] **Secrets** from a secret store, as files where it mounts them
      (`<NAME>_FILE=/run/secrets/<name>`): `SECRET_KEY` (50 random
      characters or more), the database and Redis URLs, and the JWT key if
      you use one ([security.md](security.md), "Settings and secrets").
      An env file for `docker run --env-file` lives outside the project
      (`/etc/<project>/production.env`), never next to the Dockerfile.
- [ ] **`ALLOWED_HOSTS`** names the hosts callers use, and
      **`TRUSTED_PROXIES`** the networks of your load balancers (empty
      only when callers connect directly).
- [ ] **Pools sized**: `POOL_SIZE` and `MAX_OVERFLOW` so that processes ×
      replicas × (`POOL_SIZE` + `MAX_OVERFLOW`) stays below the server's
      `max_connections` ([Sizing](operations.md#sizing)).
- [ ] **`check --deploy` in the pipeline**, with the production settings
      and the real variable names: it fails on every error and on each
      warning you have not silenced (`SILENCED_CHECKS`).

## The edge

- [ ] **TLS ends at the proxy or load balancer**, which redirects plain
      HTTP to HTTPS. The application serves plain HTTP and cannot see
      the TLS in front of it; `check` says so (`security.I001`), and
      where it takes each request's scheme from.
- [ ] **The proxy sets `X-Forwarded-For`, `X-Forwarded-Proto`, and
      `X-Request-ID`** over what a caller sent
      ([Request ids](operations.md#request-ids)).
- [ ] **The proxy limits** header sizes and slow clients
      ([Server limits](reference.md#server-limits)).
- [ ] **Probes send a `Host` of `ALLOWED_HOSTS`.** `/health` for
      liveness and `/ready` for readiness; a request with any other host
      is answered 400, a probe's included. On Kubernetes:

      ```yaml
      livenessProbe:
        httpGet:
          path: /health
          port: 8000
          httpHeaders: [{name: Host, value: api.example.com}]
      ```

## Each rollout

- [ ] **The migration job first**, from the new image, while the old code
      serves: `python manage.py migrate --plan` to review, then
      `python manage.py migrate`. If the database cannot be reached it
      says so on one line and exits with status 1; retry the job.
- [ ] **Then the web processes and the workers**, from the same image:
      `python manage.py runworker` for the outbox, at least one per
      outbox database ([The worker](operations.md#the-worker)).
- [ ] **The grace period** of the orchestrator above uvicorn's graceful
      timeout (10 seconds in the image) plus `SHUTDOWN_TIMEOUT` (15)
      ([Running the web processes](operations.md#running-the-web-processes)).

## Once it serves

- [ ] **Alerts** on what [runbook.md](runbook.md) lists: shed requests,
      the outbox backing up, dead letters, open circuit breakers, and
      logins refused while Redis is down.
- [ ] **Backups** of every database, restored once to prove they work
      ([Backups and restores](operations.md#backups-and-restores)).
- [ ] **Upgrades** one minor at a time, as [upgrading.md](upgrading.md)
      says, with `check --deploy` after each.
