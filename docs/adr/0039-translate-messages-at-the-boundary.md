# 0039. Translate messages at the HTTP boundary, from gettext catalogs compiled in Python

- Status: Accepted
- Date: 2026-10-19

## Context

Every error detail and validation message was English, whoever asked.
Django translates them with gettext: `.po` catalogs that translators edit,
compiled to `.mo` files by GNU `msgfmt`, and strings marked with `gettext`
and its lazy variants. The first other language domainkit ships is Arabic,
which has six plural forms and its own digits.

Two questions had to be answered: where a message is translated, and in
what format catalogs are kept.

**Where.** Domain errors carry their message from the domain layer, which
may import only `domainkit.ddd` ([ADR 0003](0003-enforced-layer-rules.md)).
Marking those messages with lazy strings would bring the i18n machinery
into the domain, or into `ddd` itself. The framework's own messages are
spread over modules other work changes often (`http/errors.py`, the auth
app). Every message, though, reaches the client through one function,
`error_response`, and validation messages through one handler.

**Format.** The options were:

- `.po` sources compiled to `.mo` by GNU `msgfmt`: Django's way, but a
  build tool that is not on every machine, and whose output we would
  have to trust to be the same everywhere to check it in CI.
- `.po` sources compiled to `.mo` by a compiler of our own, in Python.
- Python modules or JSON files of message dictionaries: no build step,
  but no translation tools read them, and plural forms would need a rule
  engine of our own (Arabic's rule is a formula over `n % 100`).
- Babel: it reads and writes `.po`, and brings CLDR's number and date
  data, at the cost of a new dependency for every project.

## Decision

- **Translate at the boundary.** `error_response` translates each detail,
  and the validation handler each message, into the request's language,
  by exact text. Domain and framework code pass plain English. The change
  to `http/errors.py` is two calls and a paragraph of its docstring, so
  other work on that module keeps them easily.
- **Validation messages by template.** Pydantic writes each message from a
  template per error type. Catalogs translate the templates (in the
  `validation` context), and the error's context fills them in, numbers
  written for the language. A template pydantic changes falls back to
  English; a test lists the templates we translate that pydantic no
  longer writes.
- **gettext catalogs, compiled in Python.** Catalogs are `.po` files,
  compiled to `.mo` by `domainkit.i18n.catalogs` (`manage.py
  compilemessages`), and read by the standard library's `gettext`, which
  evaluates `Plural-Forms`. The compiler's output depends only on its
  input (sorted entries, little-endian, no hash table), so the shipped
  `.mo` files are checked byte for byte against their sources, and
  `compilemessages --check` does the same for projects. It refuses a
  translation that names a placeholder its message does not have, so a
  catalog cannot break a response.
- **Lookup order.** `LOCALE_PATHS`, then each installed domain's `locale`
  directory in order, then domainkit's English and Arabic, as Django
  orders `LOCALE_PATHS`, apps, and its own.
- **The language is negotiated per request.** `LocaleMiddleware` matches
  `Accept-Language` against `LANGUAGES` and keeps the result in a context
  variable, like the request context; responses carry `Content-Language`
  and `Vary: Accept-Language`. Without the middleware, everything is
  `LANGUAGE_CODE`, so existing projects answer as before.
- **Formats by hand, from CLDR.** Numbers and dates for `en` and `ar` follow
  CLDR's data, written out in `domainkit.i18n.formats`, without Babel.
  Projects register formats for other languages.

## Consequences

Projects translate their domain errors without touching the domain
layer, with the tools translators already use, and nothing new to install.
A message built with values inside (an f-string) has no fixed text and
stays in English, as does a detail joined from several messages; the auth
app's password-rule refusals and a few admin refusals are such details
today, and need fixed texts to be translated. Every new
fixed framework message needs its English and Arabic entries: a test scans
the modules that answer requests and fails until they are there.
Catalogs are read once per process, so a recompiled catalog needs a
restart. Arabic numbers use Arabic-Indic digits, as CLDR's `ar` does; a
project that wants Latin digits registers its own formats. Caches of
translated responses must vary on the language: `LocaleMiddleware` says so
to shared caches, and `cache_page` keys entries by language.
