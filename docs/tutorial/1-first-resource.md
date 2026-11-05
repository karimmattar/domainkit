# Part 1: a first resource

In this part you add bank accounts to the project of
[part 0](0-first-route.md). A client can create accounts and list them,
but only after it logs in. This is the first of the five parts that build
the bank ([the tutorial](../tutorial.md)). Work in the `bank` directory,
with the same environment turned on.

## What you will build

- A [domain](../learning.md#domain) called `accounts`, with an `Account`
  [aggregate](../learning.md#aggregate) and its table.
- A superuser called `admin`, who may do everything.
- The routes `GET` and `POST /api/v1/accounts/accounts`, which need a
  login.

## Step 1: add a domain

A generator writes the domain for you. Then two commands make its table:

```bash
python manage.py startdomain accounts --aggregate Account
python manage.py makemigrations -m "create accounts"
python manage.py migrate
```

`startdomain` wrote the package `src/domains/accounts/`, and its tests in
`tests/domains/accounts/`. A domain is one part of the business, with its
own words and rules: here, the accounts. Its code is in four files, one
for each layer:

| File | What it holds |
| --- | --- |
| `domain.py` | The business itself: the `Account` aggregate, with a name that must not be blank, its errors, its events, and its [ports](../learning.md#port) (the [repository](../learning.md#repository) and the [unit of work](../learning.md#unit-of-work)). Plain Python, with no database and no HTTP. |
| `controllers.py` | What the domain does: one [use case](../learning.md#use-case) for each change, such as `create_account` and `rename_account`. Each one runs inside a unit of work. |
| `models.py` | How the domain is stored: the table, made from the aggregate's fields, and the SQL unit of work. |
| `routes.py` | The HTTP API: the [resource](../learning.md#resource), its use cases as routes, and who may call them. |

`check` makes sure that each file imports only what its layer may import.
For example, `domain.py` may not import the database. The words above
are explained in [the glossary](../learning.md#words).

A domain that grows large can keep one package for each layer instead
of one file: `startdomain <name> --layered`. The rules stay the same
([generators](../resources.md#generators)).

`startdomain` also changed two files of the project. It added the domain
to `INSTALLED_DOMAINS` in `src/config/settings/base.py`, and it mounted
the domain's router at `/api/v1/accounts` in `src/config/urls.py`. The
resource is at `/accounts` inside that router, so its full URL is
`/api/v1/accounts/accounts`.

`makemigrations` wrote the project's first
[migration](../learning.md#migration). It creates the table of the
accounts, and the tables of the domains every project has: users,
groups, permissions, tokens, and the outbox's messages. `migrate` applied
the migration to `db.sqlite3`.

## Step 2: create a user

Create a superuser. The first line asks for a password. You type it, and
the shell does not show it:

```bash
read -rsp "Password: " PASSWORD && export PASSWORD    # typed, not echoed
BANK_API_SUPERUSER_PASSWORD="$PASSWORD" \
  python manage.py createsuperuser --noinput --username admin --email admin@example.com
unset PASSWORD    # so that runserver, started next, does not inherit it
```

`createsuperuser --noinput` reads the password from the environment
variable `BANK_API_SUPERUSER_PASSWORD`, and prints
`Superuser created successfully.` The last line removes the password from
the shell, so that no later process can read it.

Users, groups, and permissions come installed. A superuser holds every
permission. In [part 3](3-permissions.md) you make a user who holds
fewer.

## Step 3: serve it

```bash
python manage.py runserver
```

Leave the server running, and work in the second shell.
`http://127.0.0.1:8000/docs` lists every route of the project in your
browser.

## Step 4: call it

Every route needs credentials, unless it says otherwise. Without them,
the answer is `401 Unauthorized`, with the code `not_authenticated`:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts   # 401
```

Log in with the email address, typing the password again, and keep the
token in `$TOKEN`. New projects log in with an email address or a mobile
number, not the username ([Logging in with an email address or a mobile
number](../auth/logging-in.md#logging-in-with-an-email-address-or-a-mobile-number)). The
password goes to `curl` on its standard input (`-d @-`). It is then in
no command line and in no shell history:

```bash
read -rsp "Password: " PASSWORD && export PASSWORD
TOKEN=$(python -c "import json, os; print(json.dumps({'identifier': 'admin@example.com', 'password': os.environ['PASSWORD']}))" \
  | curl -s http://127.0.0.1:8000/auth/login -H "Content-Type: application/json" -d @- \
  | python -c "import json, sys; print(json.load(sys.stdin)['access_token'])")
unset PASSWORD
```

The token starts with `dkt_`. You send it in the `Authorization` header
of each request.

Open an account. The answer is `201 Created`:

```bash
curl -i http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "Savings"}'
```

`-i` makes `curl` show the headers too. `location` is the new account's
URL, such as `/api/v1/accounts/accounts/f54caa30-...`, and `etag` is its
version, such as `W/"1-00577f472c9f6dd1"`. The body is the account:
`{"id":"f54caa30-...","version":1,"name":"Savings"}`.

List the accounts, and keep the first one's id in `$ID`. The next parts
use it:

```bash
ID=$(curl -s http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $TOKEN" \
  | python -c "import json, sys; print(json.load(sys.stdin)['items'][0]['id'])")
```

A list answers `{"items": [...], "next_cursor": null}`. `next_cursor`
is `null` because no more pages follow.

Now try a name made of blanks. The aggregate itself refuses it:
`Account.create` in `domain.py` raises `InvalidAccountName`. That class
is a `BusinessRuleViolation`, and the HTTP layer answers each one with
`422`, and a code made from the class name: `invalid_account_name`.

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "   "}'   # 422
```

The body says why:
`{"detail":"The name must not be blank.","code":"invalid_account_name",...}`.

Stop the server with Ctrl-C.

## What you learned

- `startdomain` writes a domain in four files, one for each layer, and
  installs and mounts it.
- `makemigrations` writes a migration from your classes, and `migrate`
  applies it.
- `createsuperuser` makes a user who holds every permission.
- Every route needs credentials unless it says otherwise. You log in at
  `/auth/login` and send the token as `Authorization: Bearer`.
- The aggregate holds the business rules. A `BusinessRuleViolation`
  becomes a `422` answer with a stable `code`.

Next, [part 2](2-rules.md) gives the account a balance, and you write the
rules that guard it.
