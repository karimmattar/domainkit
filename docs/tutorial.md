# Tutorial: a first service

This page goes from `pip install` to an authenticated endpoint that serves
one aggregate, in eight steps. It explains as little as it can and links to
the rest. Every command on it is run as written by the tests
(`domainkit_tests/test_walkthroughs.py`), so if one fails for you, that is a
bug: please report it.

You need Python 3.13 or 3.14, and `curl`.

## 1. Install

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install "domainkit[postgres,sqlite,testing]"
```

`sqlite` lets the project run without a database server; `postgres` is for
later, and `testing` brings the pytest plugin.

## 2. Create a project

```bash
domainkit init bankAPI bank
cd bank
python manage.py check
```

`bankAPI` is the project's name, and `bank` its directory. The name also
sets the prefix of its environment variables: `BANK_API_*`. The project
reads `config.settings.local` here (`.env` says so), which keeps its data in
`db.sqlite3`. Users, groups, and permissions are installed already.

## 3. Add a domain with one aggregate

```bash
python manage.py startdomain accounts --aggregate Account
```

`src/domains/accounts/` is a bounded context in four layers; `check` keeps
each layer to its own imports ([ADR 0003](adr/0003-enforced-layer-rules.md)):

| Layer | Holds |
| --- | --- |
| `domain/` | The `Account` aggregate: a name that must not be blank, notes as child entities, its owner by id, and the rule that an archived account cannot change. Plain Python, no I/O. |
| `application/` | The commands (`CreateAccount`, `ArchiveAccount`, ...) and the use cases that run them in a unit of work. |
| `infrastructure/` | The table and the SQLAlchemy unit of work. |
| `api/` | `routes.py`: the resource, its filters, and who may call it (`ModelActions`: the model permissions). |

`startdomain` added the domain to `INSTALLED_DOMAINS` and mounted its router
at `/api/v1/accounts` in `src/config/urls.py`. The resource is `/accounts`
within it, so its URL is `/api/v1/accounts/accounts`.

## 4. Create its table

```bash
python manage.py makemigrations -m "create accounts"
python manage.py migrate
pytest
```

`makemigrations` writes a migration from the table the aggregate maps to,
and `migrate` applies it. The domain came with tests of every layer, and
they pass.

## 5. Create a user

```bash
BANK_API_SUPERUSER_PASSWORD="correct-horse-battery" \
  python manage.py createsuperuser --noinput --username admin --email admin@example.com
```

A superuser holds every permission. Another user needs
`accounts.view_account` to read, and `add_account`, `change_account`, or
`delete_account` to change ([auth.md](auth.md#protecting-routes)).
`createsuperuser` without options asks for each value instead.

## 6. Serve it

```bash
python manage.py runserver
```

Leave it running. Open a second shell, `cd` to `bank`, and activate the
same environment. `http://127.0.0.1:8000/docs` lists every route.

## 7. Call it

Every route needs credentials unless it says otherwise. Without them the
answer is `401`:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts   # 401
```

Log in, and keep the token in `$TOKEN`:

```bash
TOKEN=$(curl -s http://127.0.0.1:8000/auth/login -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "correct-horse-battery"}' \
  | python -c "import json, sys; print(json.load(sys.stdin)['access_token'])")
```

Open an account. The answer is `201 Created`, with the new account's
`Location` and its `ETag`; the account belongs to the caller (`owner_id`):

```bash
curl -i http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "Savings"}'
```

List the accounts, and keep the first one's id in `$ID`:

```bash
ID=$(curl -s http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $TOKEN" \
  | python -c "import json, sys; print(json.load(sys.stdin)['items'][0]['id'])")
```

Add a note to it, then archive it:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/notes -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"text": "Opened at the branch"}'
curl -X POST http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/archive -H "Authorization: Bearer $TOKEN"
```

## 8. Watch the rules hold

The aggregate refuses changes once archived. The refusal is raised in
`domain/model.py` (`AccountIsArchived`, a `BusinessRuleViolation`), and the
HTTP layer answers it `422` with the code `account_is_archived`:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/notes -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"text": "Too late"}'   # 422
```

A blank name is refused the same way, by the `AccountName` value object
(`invalid_account_name`):

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "   "}'   # 422
```

## Where next

- [resources.md](resources.md): the resource router, and the generators
  that add aggregates, commands, queries, and events to the domain.
- [queries.md](queries.md): filters, pages, row locks, and savepoints.
- [auth.md](auth.md): users, permissions, tokens, and route rules.
- [testing.md](testing.md): the fixtures the generated tests use.
- [contrib.md](contrib.md): the outbox, workers, the audit trail, and
  idempotent requests.
- Before production: [reference.md](reference.md#deployment-checks) (what
  startup refuses), and [threat-model.md](threat-model.md) (what domainkit
  protects, and what is left to the project).
