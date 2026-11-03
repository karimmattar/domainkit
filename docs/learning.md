# Learning domainkit

A path through the pages in the order they pay off, the words they use,
and the commands a day of work needs.

## The path

1. **[The tutorial](tutorial.md)**, five parts of a quarter of an hour
   each: a project, a domain in four files, rules, permissions, an event
   handled by the worker, tests, and the checks production runs.
2. **[Resources](resources.md)** and **[validation](validation.md)**: more
   aggregates, queries, filters, and the rules a field or an aggregate
   keeps.
3. **[The request flow](request-flow.md)**: what happens between a request
   and its answer, and what each step costs.
4. **[Queries](queries.md)** and **[databases](databases.md)**: filters,
   pages, locks, migrations, and the tables made from your classes.
5. **[Users and permissions](auth.md)**: logins, tokens, and who may call
   which route.
6. **[Messaging](contrib/messaging.md)**: the outbox, background tasks,
   and the worker.
7. **[Deploying](deploying.md)** and **[operations](operations.md)**: from
   a project whose checks pass to one in production.

Coming from Django? [from-django.md](from-django.md) maps each Django
name to domainkit's. Stuck on a task? The [how-to recipes](recipes.md)
each solve one, tested as written.

## Words

| Word | What it means here |
| --- | --- |
| **Domain** | A part of the business with its own words and rules (accounts, billing), and the package that holds it: `domain.py`, `controllers.py`, `models.py`, `routes.py`. Django's app. |
| **Aggregate** | A thing the business changes as a whole, with an id: an account and its rules. Only its methods change it, so every way in meets the same rules. Django's model, without the database in it. |
| **Entity** | An object with an id inside an aggregate, stored with it (an order's lines). |
| **Value object** | A value with no id, compared by what it holds: an amount of money, an email address, a place. |
| **Use case** | One thing the domain does, as one `@use_case` function in `controllers.py`: it loads, calls the aggregate, and returns. The API, a script, and a test all call it. |
| **Command** | The request to run a use case, made from its parameters: what a route's body is, and what a task carries. |
| **Repository** | Where aggregates of one kind are kept and found: `get`, `add`, `filter`, `paginate`. Declared, not written. |
| **Unit of work** | One transaction over a domain's repositories: opened, changed, committed together, or rolled back. A use case runs in one. |
| **Domain event** | A fact an aggregate records (`AccountDeposited`), published after the commit. Django's signals, but only for what really happened. |
| **Handler** | A function that reacts to an event, marked `@handles`. A **durable** one is stored with the commit (the **outbox**) and run, and retried, by the **worker**. |
| **Resource** | The HTTP side of an aggregate: a `ResourceRouter` that lists, reads, and runs its use cases as routes. |
| **Rule** (access) | Who may call a route: a model permission, a login, a step-up, or a rule of your own. Every route has one; none is public by accident. |
| **Migration** | A change to the tables, generated from your classes (`makemigrations`) and applied (`migrate`). |
| **Problem code** | The machine-readable `code` of an error answer (`invalid_amount`), stable across releases; listed in [problems.md](reference/problems.md). |
| **Check** | A test of the settings or the code that `check` and startup run (`deploy.E003`); production refuses to start with an error. |
| **Tenant** | One customer of a service that serves many, whose rows, caches, and limits are its own ([tenancy.md](tenancy.md)). |
| **Step-up** | Proving it is you again (a second factor, a passkey) before a sensitive act. |
| **Idempotency key** | A header that makes a retried request answer what the first did, without doing it twice. |

## A day of work

| To | Run |
| --- | --- |
| Serve the project, reloading on changes | `python manage.py runserver` |
| Run background tasks and durable handlers | `python manage.py runworker` |
| Run the tests | `pytest` |
| Check settings, routes, and layers | `python manage.py check` |
| Start a domain | `python manage.py startdomain billing --aggregate Invoice` |
| Add an aggregate, a use case, a query, an event | `startaggregate`, `addcommand`, `addquery`, `addevent` |
| Write a migration after changing fields | `python manage.py makemigrations -m "invoice due date"` |
| Apply migrations | `python manage.py migrate` |
| See what is applied | `python manage.py showmigrations` |
| Write a data migration to fill in | `python manage.py makemigrations --empty -m "split names"` |
| Make a user who may do everything | `python manage.py createsuperuser` |
| Open a Python shell, or the database's | `python manage.py shell`, `python manage.py dbshell` |
| See the outbox | `python manage.py messages list pending`, `messages list dead` |
| Retry what failed | `python manage.py messages retry all` (or one id) |
| Load or save rows | `python manage.py loaddata`, `dumpdata` |
| Print the API's OpenAPI | `python manage.py openapi` |
| Translate messages | `python manage.py makemessages`, `compilemessages` |

Each command answers `--help` with its options.
