# 0166. Let a domain layer mark its texts, extract the project's own, and add a translation alone

- Status: Accepted
- Date: 2026-11-16
- Amends: [0003](0003-enforced-layer-rules.md), [0039](0039-translate-messages-at-the-boundary.md), [0149](0149-write-codes-and-pushes-in-their-readers-language.md)

## Context

A project that speaks Arabic first reported four gaps in 2.4's
translations, each with a workaround of its own.

**A domain layer cannot mark its texts.** ADR 0039 translates a domain
error's message at the boundary, by its exact text, so the domain layer
passes plain English and imports nothing of `domainkit.i18n`. But
`makemessages` finds only marked texts, so the refusals a domain raises,
the texts users read most, never reach a catalog unless someone copies
them by hand. The project allowed the whole of `domainkit.i18n` in its
domain layers (`ARCHITECTURE["DOMAIN_ALLOWED"]`). That package imports
the settings, the domain registry, and their dependencies (about 300
modules, against 22 for `domainkit.clock`), and its `gettext` reads
`LANGUAGE_CODE`, `LOCALE_PATHS`, and the catalog files: what ADR 0003
keeps out of a domain ("no I/O, no settings"). Marking needs none of it.
The GNU gettext manual marks a text that is translated later with a no-op
(`gettext_noop`), and Django's `makemessages` extracts it
([GNU gettext, special cases](https://www.gnu.org/software/gettext/manual/html_node/Special-cases.html),
[Django, marking strings as no-op](https://docs.djangoproject.com/en/5.2/topics/i18n/translation/#marking-strings-as-no-op)).
domainkit's `makemessages` did not, although its docs said `gettext_noop`
marks text for the catalogs. ABP, a DDD framework, has its domain throw a
code that the boundary localizes, because an entity method cannot reach a
localizer ([ABP, exception handling](https://abp.io/docs/en/abp/latest/Exception-Handling)).

**`makemessages` reads only the domains.** The project's `config`
package, which holds the settings and the root router, is never read, so
a text marked there reaches clients in English with no warning. Django's
`makemessages` puts a text from outside every app with a `locale`
directory in the first `LOCALE_PATHS` directory, and fails when there is
none ([Django, how to create language files](https://docs.djangoproject.com/en/5.2/topics/i18n/translation/#how-to-create-language-files);
[its source](https://github.com/django/django/blob/main/django/core/management/commands/makemessages.py)).
It rewrites that catalog through `msgmerge`, so an entry no code of the
project marks becomes obsolete. In domainkit that catalog is also where a
project rewords the framework's and the domains' messages (ADR 0039's
lookup order), and no code of the project marks those.

**A translation cannot be added alone.** ADR 0149 picks a mail
template's language among the files beside its text body, in the first
directory that has that body. A project that only adds
`devices/enrollment_code.ar.txt` to `MAIL.TEMPLATE_DIRS` is not read: it
copied the English body beside it, word for word. The same rule makes a
project's English-only override win over the framework's translation of
the template it replaces, which ADR 0149 chose on purpose (user report
A11 of the second report).

**`LocaleMiddleware` kept `LANGUAGE_CODE` and `LANGUAGES` from when the
application was built,** so a test that overrides them is served the old
ones. Django reads `LANGUAGE_CODE` on every request, and clears its cache
of `LANGUAGES` when a setting changes
([trans_real.py](https://github.com/django/django/blob/main/django/utils/translation/trans_real.py)).

## Decision

**A domain layer may import `domainkit.i18n.lazy`.** It holds the
functions that look nothing up when called: `gettext_noop`, which
returns its text, and `gettext_lazy`, `ngettext_lazy`, `pgettext_lazy`,
which make a `LazyString` looked up when it is shown. The lookups are
imported on first use, and `domainkit.i18n` imports its names on first
use too (PEP 562, as `domainkit.http` does since PERF-15), so importing
`domainkit.i18n.lazy` loads no settings. `DOMAIN_ALLOWED_PREFIXES`
lists it; the rest of `domainkit.i18n` stays forbidden in a domain. A
refusal is marked with `gettext_noop` and the boundary translates it, as
ADR 0039 decided; a refusal with values is translated, then filled
(`gettext_lazy(...).format(...)`), since the boundary finds no filled
text in a catalog; a label is lazy. A lazy string given to an error is
answered as its text. `makemessages` extracts `gettext_noop`. The options weighed: allowing the whole package (the
project's workaround, and the report's proposal), which lets a domain
read the settings; marking functions in `domainkit.ddd`, which ADR 0039
kept free of translation; or error codes the boundary maps to texts, as
ABP does, which every domain error would have to grow.

**`makemessages` reads the project's own modules.** With no domain
labels, it also reads the package that holds the settings module and the
one that holds `ROOT_ROUTER`'s module (a module outside any package
alone), leaving out domainkit's and every installed domain's modules.
What they mark is *added* to the catalogs of the first `LOCALE_PATHS`
directory, which is looked up first at run time: nothing already in them
changes or becomes obsolete, so a project's rewordings survive; an
obsolete entry of a message marked again comes back, as for a domain.
Without `LOCALE_PATHS`, a run names on stderr the messages that no
installed catalog translates already, and their files, and writes the
domains' catalogs as before; `--check` fails on them. Failing every
run, as Django does, would break a project's `makemessages` in a minor
release; reading the whole working directory, as Django does, would read
tests and scripts that serve nobody.

**A directory may hold a translation alone.** `find_template` takes the
first directory that has the template's text body *or* that body in the
active language (or its primary subtag). The report proposed looking for
the language's file in every directory before any body; that would let
the framework's Arabic replace a project's English override, which ADR
0149 refused. The order here keeps that rule and adds only directories
that hold a translation and no body, which were passed over before.

**`LocaleMiddleware` reads `LANGUAGE_CODE` and `LANGUAGES` per request.**
Both are attributes the settings proxy keeps, so the read costs no call;
the codes of `LANGUAGES` are worked out again only when the setting is
another object. The calls and modules per request of the `template`,
`me`, and `throttled` rungs are those of 2.4.0.

**Devices' mails ship in Arabic**, and a test holds every mail template
domainkit ships to have, for each language its catalogs translate, a
text body, and a subject and HTML body where the template has them, each
taking the same values.

## Consequences

- A domain's refusals and labels reach its catalog with `makemessages`,
  and its tests still need no settings: `gettext_noop` returns English.
  Turning a lazy label into text needs the settings, as before.
- `import domainkit.i18n` no longer imports its modules; each name, and
  each module, is imported when first read, and is the same object.
- A project's catalog in `LOCALE_PATHS` keeps every entry it has. A
  message the project no longer marks stays there until removed by hand.
- A project with marked texts outside its domains and no `LOCALE_PATHS`
  sees `makemessages --check` fail after upgrading, until it sets
  `LOCALE_PATHS` or moves the texts into a domain.
- A project's translation-only template directory, harmless before, is
  now read for its language.
- Each new mail template domainkit ships needs its Arabic files.
