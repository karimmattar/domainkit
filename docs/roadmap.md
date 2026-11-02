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

## Next

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
