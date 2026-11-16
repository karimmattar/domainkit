# 0149. Write codes and pushes in their reader's language

- Status: Accepted
- Date: 2026-11-16

## Context

A project that serves English and Arabic (`LANGUAGES`) reported that
everything domainkit sends a person was English: the text messages with
a reset, verification, signup, or enrollment code (f-strings in
`auth.recovery.text_code`), the mails with codes (English templates),
the time a code works for (`10 minutes`), and ciba's push title and body
(`CIBA.PUSH_TITLE`, `PUSH_BODY`). Responses to requests were already in
the caller's language (`LocaleMiddleware`, ADR 0039).

Two questions: whose language, and how each kind of message carries it.

**Whose language.** A message is read by its recipient, not by whoever
made the request: a reset can be asked for from any browser, and a CIBA
sign-in is started by a client's server. Identity servers order it so:
Keycloak's locale selector reads the user's stored locale before the
request's `Accept-Language`, then falls back to the realm's default
([Red Hat SSO 7.4, Locale Selector](https://docs.redhat.com/ja/documentation/red_hat_single_sign-on/7.4/html/server_developer_guide/_locale_selector)).
Django stores no language on its user; projects add a field and wrap the
sending in `translation.override(user.language)`
([Django, translations outside views](https://docs.djangoproject.com/en/5.2/topics/i18n/translation/#using-translations-outside-views-and-templates)).
domainkit's user has no such field either. Codes are sent by the worker,
where no request is active, so the request's language must travel with
the task: the usual Celery advice is to pass it as an argument
([Fueled, multilingual Django, part 3](https://fueled.com/the-cache/posts/backend/django/becoming-a-multilingual-super-hero-in-django-part-3/)).

Options weighed for the user's language:

1. **A `language` column on `AbstractUser`.** Every project would get a
   migration, a field in the admin API, and a value to keep up to date,
   for a feature many do not need. Hard to take back.
2. **A setting naming a hook that returns the language.** Flexible, but
   one more setting for what is a property of the user model.
3. **A class attribute naming the field, like `EMAIL_FIELD`.** Opt-in, no
   migration, no setting; a project that stores a language says where.

**How texts are translated.** Through domainkit's gettext catalogs, as
error details are: whole sentences with `{placeholders}`, plural forms
for the duration (Arabic has six). The purpose of a code ("reset your
password") completes a sentence; it is translated in the `code` context,
so a project that sends its own codes (`send_code(wording=...)`) adds its
wording to its catalogs the same way.

**How mails are translated.** Mail templates have no tags
(`contrib.mail.templates`: "compute such text in Python"), so gettext
cannot reach inside them. Options:

1. **Move the prose into the catalogs and pass it in as values.** The
   templates become a list of `{{ paragraph }}` names, and projects that
   copied them lose what they read.
2. **One file per language beside the template**, as Rails picks
   `index.es.html.erb` when the locale is `es`
   ([Rails i18n guide, localized views](https://guides.rubyonrails.org/i18n.html)).
   Translators work on whole messages, and a project overrides one
   language or all.

**How pushes are translated.** FCM and APNs can carry localization keys
(`title_loc_key`, `title-loc-key`) that the phone resolves from the
app's own strings, in the phone's language
([FCM](https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages),
[APNs](https://developer.apple.com/documentation/usernotifications/generating-a-remote-notification)).
That needs every app to ship those keys, differs per provider, and gives
a project's own `PUSH_TITLE` no key at all. Translating on the server
needs only the catalogs.

## Decision

A message to a person is written in, in order:

1. the user's language, where the user model names its field
   (`AbstractBaseUser.LANGUAGE_FIELD`, `None` by default; startup refuses
   a name the model lacks);
2. else the language of the request that asked, carried to the task as
   its `language` keyword argument, and only when it is not
   `LANGUAGE_CODE`, so tasks queued as before keep their arguments;
3. else `LANGUAGE_CODE`.

Only a language `LANGUAGES` lists counts; `ar-eg` is `ar` where only
`ar` is listed. `auth.integration.language_of(user, requested)` decides,
and `language_argument()` gives a task the request's.

Texts and the duration come from the gettext catalogs; ciba's push title
and body are looked up there too, so domainkit translates its defaults
and a project's catalogs may translate its own. Mail templates take files
per language beside them (`auth/password_reset_code.ar.txt`, with its own
`.ar.subject.txt` and `.ar.html`), used while that language is active,
then its primary subtag's; a language's files are the whole template. The
directory that holds the template's own text body decides, so a project's
override in `MAIL.TEMPLATE_DIRS` wins over the translations of the
template it replaces. Pushes carry no localization keys.

## Consequences

- **A client that asks in Arabic gets its codes in Arabic,** with the
  default `LANGUAGES` and `LocaleMiddleware`. A project that wants one
  language lists only it.
- **A project that stores a language names its field,** and every code
  and push follows it, whoever asks.
- **A project's own templates and push text stay as they are,** until it
  adds its translations beside them, or to its catalogs.
- **A task queued in another language than `LANGUAGE_CODE` carries
  `language`.** A worker of 2.3 fails on it and retries the message:
  deploy workers first ("Upgrading", 2.3 to 2.4).
- **Each new text sent to people needs its Arabic entry,** as each error
  detail does, and each new code mail its Arabic template.
- **Not covered:** the devices app's mail templates (`devices/*`: its
  enrollment code, its notices) and `PHONES.MESSAGE`, a setting with its
  own text. They are picked in the same language, and stay English until
  they get translations of their own.
