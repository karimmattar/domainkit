# Roadmap: an easy domainkit

What building a first application on 1.0 showed was hard, and the plan to
make it easy, in the order it pays off. Each step keeps the rules that make
domainkit safe (layers, deny by default, exact money) and removes ceremony
around them.

## Done in 1.5

| Need | What 1.5 gives |
| --- | --- |
| A domain is many packages and files | `startdomain` writes four files, one per layer: `domain.py`, `controllers.py`, `models.py`, `routes.py`. `--layered` keeps packages for large domains. |
| A custom user model takes six manual steps, and the docs put it where `check` refuses it | `startdomain accounts --user` writes it, installs it, and fills in `AUTH`; the test settings keep it. |
| A use case is a command class, a service, a schema, and a route line | `@use_case` on one async function; `ResourceRouter.use_case` makes its route. |
| One aggregate's storage is eight hand-written pieces | The unit of work's annotations: SQL repositories and `in_memory(...)` for tests are made from them. |
| Only `MaxLength` limits text | `MinLength`, `Pattern`, and `Range`, checked in request bodies and shown in OpenAPI. |

## First: revamp the core

Building an application on 1.0 showed the core is hard to follow before it
is hard to extend. Before more features, each part of the core gets one
obvious way to do each task, measured on the benchmark ladder and covered
at 98% or more:

1. **Structure.** One public home per name, and one shape per kind of
   thing (the audit's DSGN-29 to -33: names with two homes, three shapes of
   management command, two encryption systems, apps that refuse a
   deployment without a check id). Fewer layers of indirection between a
   route and the database.
2. **Data flow.** One path a request takes, written down and drawn:
   route, command, use case, aggregate, unit of work, events. Commands and
   routes derived from a use case's signature, so a use case is written
   once instead of three times (command class, service, route line).
3. **Validation.** Field markers (`MaxLength`, `MinLength`, `Pattern`,
   `Range`) checked by the aggregate itself when it is made and changed,
   not only by request bodies, with Django's `clean()` as the hook for
   rules across fields; every refusal a 422 with the field's name.
4. **Speed.** Budgets on the ladder for the common paths (a read, a
   write, a list page), and the per-request work that does not depend on
   the request moved to startup.
5. **Django's conveniences**, where a domain-driven shape allows them:
   `get_or_create` and `update_or_create`, bulk writes, aggregations
   (`Count`, `Sum`) on a repository, choices from an `Enum` with labels,
   constraints and indexes declared on the aggregate (`Meta`), and the
   commands people reach for first (`inspectdb`, `sqlmigrate`). Each is
   checked against what the repository and `table_for` already do before
   it is built.

### What the core asks of an application today

Measured on the guide's shop, one aggregate with four commands:

| To add | An application writes today | Where |
| --- | --- | --- |
| One use case | A `Command` class, a service function, a request schema (`schema_for`), and a `command_route` line | four places, two files |
| One aggregate's storage | A repository port, a unit-of-work port, a SQLAlchemy repository, a SQLAlchemy unit of work, `table_for`, `map_imperatively`, `start_mappers`, and an in-memory fake for tests | eight pieces |
| One validated field | A marker for the API (`MaxLength`), a value object for the domain, and, on a user, `create` and `clean_field` overrides | three mechanisms |
| A custom user field | The class, a hand-written column, `EDITABLE_FIELDS`, and conftest overrides of `admin_client` and `client_for` | four places |

### The target design

- **A use case is one function** (done in 1.5). `@use_case` on `async def
  restock(uow, product_id: UUID, quantity: Annotated[int, Range(min=1)]) ->
  Product` is enough: the command, its request schema, its route, its
  OpenAPI, and its permission (`change_product`) are derived from the
  signature, and the unit of work is opened and committed around it.
  `Command` classes stay for messages that travel (tasks, approvals).
- **An aggregate's storage is declared, not written** (done in 1.5). The
  unit of work's annotations (`products: ProductRepository`) make the SQL
  repositories and an in-memory twin for tests (`in_memory`); a repository
  class of its own is written only when the port adds queries. Tables stay
  one `table_for` line each.
- **One validation path.** Field markers and a `clean()` method are
  checked by the aggregate itself when it is made or changed (the API
  derives its schema from the same markers), and the user model is an
  aggregate like any other, so `phone: Annotated[str, Pattern(...)]` is the
  whole of a validated user field.
- **One request flow, drawn in the docs**: route, controller, unit of
  work, aggregate, events after commit, outbox. Nothing else on the path
  that a reader cannot see.
- **Migration**: each step lands behind the current API, the guide's shop
  is rewritten on it and must shrink, and the old spelling is removed in
  the release after.

## Then

1. **Generators for the four-file layout.** `addcommand`, `addquery`, and
   `addevent` grow layered domains only; teach them `controllers.py` and
   `routes.py`, then make the tutorial use the simple layout.
2. **Field rules that hold everywhere.** Today `MinLength`, `Pattern`, and
   `Range` are checked at the API; a value object checks itself everywhere.
   Let an aggregate check its markers when it is made and changed, so a
   script or a task meets the same limits, with the error as a 422.
3. **Custom user fields end to end.** Auth's signup takes only a username
   and an email: let it take the user model's `REQUIRED_FIELDS`, and let
   `clean_field` and `create` checks come from field markers, so a phone
   number needs one line.
4. **One page per extension point**, each with a working, tested example:
   an authentication class (API keys), a permission rule, a cache backend,
   a broker, an SMS or mail backend, and settings of a domain's own
   (`settings_class`). The interfaces exist (`BaseAuthentication`,
   `BasePermission`, `CACHES`, `BROKERS`, `SmsBackend`, `DomainConfig`);
   what is missing is the short, copyable recipe.
5. **Phone verification as an app.** A `contrib.phones` with the code, its
   hash, expiry, tries, and the SMS through the outbox, as the guide's
   example builds by hand.
6. **A learning path in plain words**: the tutorial on the four-file
   layout, a glossary, and the commands a day of work needs.
