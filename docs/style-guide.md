# How these pages are written

This page is for people who write or change domainkit's documentation.
It says how a page is built and how its sentences are written. Many
readers do not speak English as their first language, so the rules below
put clear words before short or clever ones.

## Four kinds of page

Every page is one of four kinds. A page does one job only.

| Kind | Its job | The reader is | Example |
| --- | --- | --- | --- |
| **Tutorial** | Teach by building one thing, step by step. | New, and learning. | [Getting started](tutorial.md) |
| **Guide** | Explain one part of domainkit fully: what it is, how to use it, every option. | Building a service. | [Validation](validation.md) |
| **How-to** | Solve one task, from start to finish. | Busy, with one goal. | [A payment that runs again](recipes/payments.md) |
| **Reference** | List facts to look up: settings, commands, errors, names. | Looking one thing up. | [Settings](reference/settings.md) |

A guide may link to the reference for the full list of options. It does
not copy the list.

## The parts of a guide

A guide has these parts, in this order. Leave out a part only when it
has nothing to say.

1. **A title** that names the subject: "Validation", "Caches". Not a
   sentence, not a question.
2. **An opening paragraph** of two to four sentences. It says what the
   feature does, and why a service needs it.
3. **"On this page"**: a short bullet list of what the reader will learn
   or be able to do.
4. **A first example**: the smallest complete code that works. A reader
   who copies it and runs it gets a result.
5. **Sections**, one per idea, from the most used to the least used. Each
   section starts with one sentence that says what it is about, then shows
   code, then explains the code.
6. **Options**: a table of every setting, argument, or marker the feature
   takes, with its default and what it does. Link to the reference page
   for the full setting rows.
7. **Errors and refusals**: what goes wrong, what the reader sees (the
   exception, the HTTP status, the problem `code`), and how to fix it.
8. **Common mistakes**: two to five things people get wrong, each with the
   fix.
9. **See also**: links to the guides, how-tos, and reference pages that
   come next.

A tutorial page has an opening paragraph, "What you will build", steps
with numbers, and "What you learned" at the end. A how-to has "The
problem", "The solution" in steps, and "How it works".

## Code

- **Every Python example parses, and every `from domainkit... import`
  in it names something domainkit has.** A test checks this
  (`test_every_python_snippet_parses_and_imports_what_is_there`). Write
  `...` only where Python allows it: as a value (`x = ...`) or as a body
  (`def f() -> None: ...`). Never write `f(...)` or `{..., "key": 1}` as
  a shortcut.
- **Show the imports.** The first example of a page is complete: its
  imports, its classes, and its call. Later examples may leave out what
  the page already showed.
- **Show the result.** After code that prints, returns, or answers,
  show what comes out: the value, the JSON body, or the command's output.
  Run the code first, and copy the real output.
- **Use the names the tutorial uses** where they fit: a shop with
  products, orders, and customers; a bank with accounts and payments.
  Ids are `NewType`s of `UUID`: `ProductId = NewType("ProductId", UUID)`.
- **Mark the language** of every code block: `python`, `bash`, `text`,
  `json`, `toml`. Use `bash` for commands the reader types, and `text`
  for output.
- **Keep examples short.** Ten to thirty lines. Split a long example into
  steps.

## Sentences

- **One idea per sentence.** Aim for sentences of fewer than 20 words. Two
  short sentences are better than one long one.
- **Use the active voice.** Write "The unit of work commits the rows", not
  "The rows are committed by the unit of work".
- **Address the reader as "you".** Write "You set `CACHES`", not "One sets
  `CACHES`" or "The user sets `CACHES`".
- **Use common words.** Write "use", not "utilize"; "start", not
  "commence"; "about", not "approximately"; "because", not "since" or
  "as" for a reason.
- **Avoid idioms and phrasal verbs that a translator cannot guess.** Write
  "find", not "look up"; "remove", not "get rid of"; "is not
  possible", not "is a no-go". Simple verb + preposition pairs that are
  in every dictionary ("set up", "log in") are fine.
- **Say the subject of every sentence.** Do not start with "It" or "This"
  when the sentence before had two possible subjects.
- **Keep one word for one thing.** A "unit of work" is never also a
  "transaction object". [The glossary](learning.md#words) gives each
  word; link to it the first time a page uses a domainkit word.
- **Write numbers as digits** (3, 15 minutes, 1,000) and units in full
  (seconds, not secs).
- **Do not write "simply", "just", "easily", "obviously", or "of course".**
  What is easy for the writer may not be easy for the reader.
- **Do not refer to Django to explain a feature.** Explain the feature
  first. A short note, "Django calls this `pre_save`", may follow for
  readers who know Django.

## Headings

- Use sentence case: "Rules across fields", not "Rules Across Fields".
- Name the topic, not the code alone: "Rules across fields: `clean()`" is
  better than "`clean()`".
- Keep a heading stable once other pages, the code, or error messages
  link to it. A test checks that every section the code names is still
  there (`test_every_page_the_code_names_has_the_section_it_names`), and
  another that every link leads somewhere.

## Notes and warnings

Use an admonition for something the reader must not miss:

```text
!!! warning
    `flush` deletes every row. Run it only on a database you can lose.
```

Use `note` for extra facts, `tip` for a better way, and `warning` for
data loss, security, or money. Use no more than three on a page.

## What every page is checked for

`pytest domainkit_tests/test_docs.py domainkit_tests/test_api.py
domainkit_tests/test_walkthroughs.py` checks:

- every link and anchor leads somewhere;
- every setting, command, and password validator is documented;
- every section that code or error messages name exists;
- every Python snippet parses and imports what exists;
- the API pages list each module's public names, in order, with the
  first line of each docstring;
- the tutorial's commands and edits still build a working service.
