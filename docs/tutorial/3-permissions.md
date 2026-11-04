# Part 3: users and permissions

Until now, only the superuser has called the API, and a superuser holds
every permission. In this part you make a clerk who may read accounts
and do nothing more. You also see where the rule that decides this
lives.

## What you will build

- A user called `clerk`, made through the admin API.
- One permission for the clerk: `accounts.view_account`.
- Proof that the clerk can list accounts, but cannot deposit.

## Step 1: see who may call a route

Each route of the accounts [resource](../learning.md#resource) needs a
model [permission](../learning.md#permission). `startdomain` wrote this
[access rule](../learning.md#access-rule) into
`src/domains/accounts/routes.py`:

```text
accounts = ResourceRouter(..., rule=ModelActions(Account))
```

`ModelActions(Account)` asks for a different permission for each kind of
route:

| Route | Permission it needs |
| --- | --- |
| A read: the list, or one account | `accounts.view_account` |
| A command that creates, such as `POST /accounts` | `accounts.add_account` |
| A `DELETE` command | `accounts.delete_account` |
| Every other command, such as `DepositAccount` | `accounts.change_account` |

A permission's name is the domain's label, a dot, the action, and the
model's name: `accounts.view_account`.

A caller who proved nothing gets `401`. A caller who logged in but lacks
the permission gets `403`, with the code `permission_denied`. A route
with no rule at all is never public by accident: `check` lists it, and
staging and production refuse to start
([deny by default](../auth/routes.md#deny-by-default)).

## Step 2: make a clerk

Serve the project again:

```bash
python manage.py runserver
```

The project mounts the admin API at `/admin/auth`, in
`src/config/urls.py`. The admin API manages users, groups, and
permissions over REST, for staff. From the second shell, as the
superuser (`$TOKEN`), make a user called `clerk`. You type a password
for the clerk, and you keep the clerk's id in `$CLERK`:

```bash
read -rsp "Clerk's password: " CLERK_PASSWORD && export CLERK_PASSWORD
CLERK=$(python -c "import json, os; print(json.dumps({'username': 'clerk', 'password': os.environ['CLERK_PASSWORD']}))" \
  | curl -s http://127.0.0.1:8000/admin/auth/users -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" -d @- \
  | python -c "import json, sys; print(json.load(sys.stdin)['id'])")
```

`POST /admin/auth/users` answers `201 Created` with the new user.

Log in as the clerk, keep the clerk's token in `$CLERK_TOKEN`, and then
remove the password from the shell:

```bash
CLERK_TOKEN=$(python -c "import json, os; print(json.dumps({'username': 'clerk', 'password': os.environ['CLERK_PASSWORD']}))" \
  | curl -s http://127.0.0.1:8000/auth/login -H "Content-Type: application/json" -d @- \
  | python -c "import json, sys; print(json.load(sys.stdin)['access_token'])")
unset CLERK_PASSWORD
```

## Step 3: grant what the clerk needs

The clerk holds no permission yet, so the accounts are closed to them:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $CLERK_TOKEN"   # 403
```

The answer is `403`, with the code `permission_denied` and the detail
`You do not have permission to perform this action.`

As the superuser, grant the clerk `accounts.view_account`:

```bash
curl -X PUT http://127.0.0.1:8000/admin/auth/users/$CLERK/permissions -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"permissions": ["accounts.view_account"]}'
```

`PUT .../permissions` sets exactly the permissions you send, and removes
any others the user held directly. The answer is the user, with
`"permissions":["accounts.view_account"]`.

Now the clerk can read the accounts. A deposit needs
`accounts.change_account`, so it is still refused:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $CLERK_TOKEN"
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/deposit -H "Authorization: Bearer $CLERK_TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": {"amount": "5.00", "currency": "USD"}}'   # 403
```

The list shows both accounts, `Old savings` and `Savings`. The deposit
gets `403 permission_denied`.

A grant works at once in the process that made it. With several
processes, each process keeps what a token may do for up to 5 seconds,
unless a shared cache tells them all at once
([the principal cache](../auth/authentication.md#the-principal-cache)).

Stop the server.

## What you learned

- An access rule on each route decides who may call it.
  `ModelActions(Account)` asks for `view`, `add`, `change`, or `delete`
  on the model, by the kind of route.
- No credentials get `401`. Credentials without the permission get
  `403`.
- The admin API at `/admin/auth` makes users and grants permissions.

For a team, grant permissions to a group with
`PUT /admin/auth/groups/{id}/permissions`, and put users in the group
with `PUT /admin/auth/users/{id}/groups`. Other rules (`IsAuthenticated`,
scopes, permissions on single objects, and rules of your own) are in
[protecting routes](../auth/routes.md#protecting-routes).

Next, [part 4](4-events.md) reacts to each deposit, outside the request.
