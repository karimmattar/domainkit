# The commands contributors run, the way CI runs them (CONTRIBUTING.md).
# "make" lists them. Each runs in the Poetry environment ("make install"
# makes it); domainkit_tests/test_ci.py checks that "lint" runs what CI's
# lint job runs, and that "integration" sets what its integration job sets.

POETRY ?= poetry
RUN := $(POETRY) run
# A failing step fails its target, inside a pipe too.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

# CI's integration job, against the servers of compose.yaml.
INTEGRATION_ENV := \
	COVERAGE_FILE=.coverage.integration \
	DOMAINKIT_TEST_POSTGRES_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/postgres \
	DOMAINKIT_TEST_IMMUDB_URL=immudb://immudb:immudb@localhost:3322/defaultdb \
	DOMAINKIT_TEST_IMMUDB_CA=.ci/immudb/server.crt \
	DOMAINKIT_TEST_IMMUDB_SIGNING_KEY=.ci/immudb/signing.pub \
	DOMAINKIT_TEST_REDIS_URL=redis://localhost:6379/0 \
	DOMAINKIT_TEST_REDIS_TLS_URL=rediss://localhost:6380/0 \
	DOMAINKIT_TEST_REDIS_CA=.ci/redis/server.crt \
	DOMAINKIT_TEST_VALKEY_URL=redis://localhost:6381/0 \
	DOMAINKIT_TEST_RABBITMQ_URL=amqp://domainkit:domainkit@localhost:5672/ \
	DOMAINKIT_TEST_RABBITMQ_TLS_URL=amqps://domainkit:domainkit@localhost:5671/ \
	DOMAINKIT_TEST_RABBITMQ_CA=.ci/rabbitmq/server.crt \
	DOMAINKIT_TEST_RABBITMQ_MANAGEMENT_URL=http://domainkit:domainkit@localhost:15672

.DEFAULT_GOAL := help
.PHONY: help install lint fix test integration services services-down cov bench docs docs-serve audit mutants check

help: ## List the commands.
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | expand -t 16

install: ## The environment: every extra, the dev and docs groups, and the hooks.
	$(POETRY) sync --all-extras --with dev,docs
	pre-commit install  # After: pipx install pre-commit

lint: ## What CI's lint job runs: the lock, ruff, formatting, mypy, deptry.
	$(POETRY) check --lock
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) mypy
	$(RUN) deptry .

fix: ## Let ruff fix and format what it can.
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

test: ## The suite, on SQLite and fakes, in random order, with coverage.
	$(RUN) pytest --cov

services: ## Start the servers of compose.yaml, as CI's integration job has them.
	docker compose up -d --wait
	@for _ in $$(seq 1 60); do \
	  docker compose logs immudb 2>&1 | grep -q "you can now use" && exit 0; sleep 1; \
	done; docker compose logs immudb; exit 1

services-down: ## Stop the servers, and drop their data.
	docker compose down -v

integration: services ## The suite against the servers; a skip fails, as in CI.
	$(INTEGRATION_ENV) $(RUN) pytest --cov -rs | tee pytest.log
	@! grep -q "SKIPPED" pytest.log

cov: ## Combine the runs with and without servers; CI requires 100%.
	$(RUN) coverage combine --keep .coverage .coverage.integration
	$(RUN) coverage report --fail-under=100

bench: ## The benchmark ladder against its budgets (docs/performance.md).
	$(RUN) python -m benchmarks --strict

docs: ## Build the documentation site, links and API names checked, into site/.
	NO_MKDOCS_2_WARNING=1 $(RUN) mkdocs build --strict --site-dir site

docs-serve: ## Serve the documentation site, rebuilt on each change.
	NO_MKDOCS_2_WARNING=1 $(RUN) mkdocs serve

audit: ## Known vulnerabilities in the locked packages, and secrets in the history.
	$(RUN) pip-audit --progress-spinner off --ignore-vuln PYSEC-2026-1325
	gitleaks git --redact --no-banner .

mutants: ## Mutation testing of one area: make mutants MUTANTS="domainkit.http*".
	$(RUN) mutmut run $(MUTANTS)
	$(RUN) mutmut results

check: lint test docs ## Everything a pull request needs before CI.
