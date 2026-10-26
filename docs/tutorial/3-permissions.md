# Part 3: users and permissions

So far only the superuser has called the API, and a superuser holds every
permission. This part makes a clerk who may read accounts and nothing
more, and shows where the rule that decides it lives.

## Who may call a route

Each route of the accounts resource needs a model permission, which
`startdomain` wrote into `src/domains/accounts/api/routes.py`:

```text
accounts = ResourceRouter(..., rule=ModelActions(Account))
```

`ModelActions(Account)` asks for `accounts.view_account` to read,
`accounts.add_account` to create, `accounts.delete_account` to delete, and
`accounts.change_account` for every other command, `DepositAccount`
included. A caller who proved nothing gets `401`; one who logged in but
lacks the permission gets `403`. A route with no rule at all is listed by
`check` and refused in production, so none is public by accident
([deny by default](../auth.md#deny-by-default)).

## Make a clerk

Serve the project again:

```bash
python manage.py runserver
```

The project mounts the admin API at `/admin/auth` (`src/config/urls.py`):
users, groups, and permissions over REST, for staff. From the second
shell, as the superuser (`$TOKEN`), make a user `clerk`, typing a password
for them, and keep their id in `$CLERK`:

```bash
read -rsp "Clerk's password: " CLERK_PASSWORD && export CLERK_PASSWORD
CLERK=$(python -c "import json, os; print(json.dumps({'username': 'clerk', 'password': os.environ['CLERK_PASSWORD']}))" \
  | curl -s http://127.0.0.1:8000/admin/auth/users -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" -d @- \
  | python -c "import json, sys; print(json.load(sys.stdin)['id'])")
```

Log in as the clerk, and forget the password:

```bash
CLERK_TOKEN=$(python -c "import json, os; print(json.dumps({'username': 'clerk', 'password': os.environ['CLERK_PASSWORD']}))" \
  | curl -s http://127.0.0.1:8000/auth/login -H "Content-Type: application/json" -d @- \
  | python -c "import json, sys; print(json.load(sys.stdin)['access_token'])")
unset CLERK_PASSWORD
```

## Grant what the clerk needs

The clerk holds no permission yet, so the accounts are closed to them:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $CLERK_TOKEN"   # 403
```

Grant them `accounts.view_account`, as the superuser:

```bash
curl -X PUT http://127.0.0.1:8000/admin/auth/users/$CLERK/permissions -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"permissions": ["accounts.view_account"]}'
```

Now the clerk reads the accounts, but a deposit, which needs
`accounts.change_account`, is still refused:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $CLERK_TOKEN"
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/deposit -H "Authorization: Bearer $CLERK_TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": "5.00"}'   # 403
```

A grant takes effect at once in this process. With several processes,
each keeps what a token may do for a few seconds, unless a shared cache
tells them all ([auth.md](../auth.md#the-principal-cache)).

Stop the server. For a team, grant permissions to a group
(`PUT /admin/auth/groups/{id}/permissions`) and put users in it. Rules
other than model permissions (`IsAuthenticated`, scopes, object
permissions, rules of your own) are in
[auth.md](../auth.md#protecting-routes). [Part 4](4-events.md) reacts
to deposits, outside the request.
