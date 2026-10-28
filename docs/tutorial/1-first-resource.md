# Part 1: install, and a first resource

This part goes from `pip install` to an endpoint that creates and lists
bank accounts, behind a login. It is the first of five
([the path](../tutorial.md)); each part ends with a project that works.

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
keeps its data in `db.sqlite3`. Users, groups, and permissions are
installed already.

## Add a domain

```bash
python manage.py startdomain accounts --aggregate Account
python manage.py makemigrations -m "create accounts"
python manage.py migrate
```

`src/domains/accounts/` is a bounded context in four layers. `check` keeps
each layer to its own imports:

| Layer | Holds |
| --- | --- |
| `domain/` | The `Account` aggregate: a name that must not be blank, notes, its owner, and the rule that an archived account cannot change. Plain Python, no I/O. |
| `application/` | The commands (`CreateAccount`, `ArchiveAccount`, ...) and the use cases that run them in a unit of work. |
| `infrastructure/` | The table, derived from the aggregate's fields, and the SQLAlchemy unit of work. |
| `api/` | `routes.py`: the resource, its filters, and who may call it. |

`startdomain` added the domain to `INSTALLED_DOMAINS` and mounted its
router at `/api/v1/accounts` in `src/config/urls.py`. The resource is
`/accounts` within it, so its URL is `/api/v1/accounts/accounts`.
`makemigrations` wrote a migration for the table the aggregate maps to, and
`migrate` applied it.

## Create a user

```bash
read -rsp "Password: " PASSWORD && export PASSWORD    # typed, not echoed
BANK_API_SUPERUSER_PASSWORD="$PASSWORD" \
  python manage.py createsuperuser --noinput --username admin --email admin@example.com
unset PASSWORD    # so that runserver, started next, does not inherit it
```

A superuser holds every permission. [Part 3](3-permissions.md) makes a user
who holds fewer.

## Serve it

```bash
python manage.py runserver
```

Leave it running. Open a second shell, `cd` to `bank`, and activate the
same environment. `http://127.0.0.1:8000/docs` lists every route.

## Call it

Every route needs credentials unless it says otherwise. Without them the
answer is `401`:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts   # 401
```

Log in, typing the password again, and keep the token in `$TOKEN`. The
password goes to `curl` on its standard input (`-d @-`), so it is in no
command line and no shell history:

```bash
read -rsp "Password: " PASSWORD && export PASSWORD
TOKEN=$(python -c "import json, os; print(json.dumps({'username': 'admin', 'password': os.environ['PASSWORD']}))" \
  | curl -s http://127.0.0.1:8000/auth/login -H "Content-Type: application/json" -d @- \
  | python -c "import json, sys; print(json.load(sys.stdin)['access_token'])")
unset PASSWORD
```

Open an account. The answer is `201 Created`, with the new account's
`Location` and its `ETag`; the account belongs to the caller (`owner_id`):

```bash
curl -i http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "Savings"}'
```

List the accounts, and keep the first one's id in `$ID` for the next parts:

```bash
ID=$(curl -s http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $TOKEN" \
  | python -c "import json, sys; print(json.load(sys.stdin)['items'][0]['id'])")
```

A name of blanks is refused by the aggregate itself: the `AccountName`
value object in `domain/model.py` raises `InvalidAccountName`, a
`BusinessRuleViolation`, which the HTTP layer answers `422` with the code
`invalid_account_name`:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "   "}'   # 422
```

Stop the server with Ctrl-C. Next, [part 2](2-rules.md) gives the account a
balance and writes the rules that guard it.
