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
