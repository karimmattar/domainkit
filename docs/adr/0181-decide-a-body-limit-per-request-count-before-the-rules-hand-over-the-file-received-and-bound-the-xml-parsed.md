# 0181. Decide a body limit per request, count before the rules, hand over the file received, and bound the XML parsed

- Status: Accepted
- Date: 2026-11-17
- Amends: [0175](0175-bound-page-content-check-an-acts-upload-once-and-read-a-signed-body-last.md) and [0176](0176-bound-what-a-pdf-decodes-list-placeholders-fill-drafts-and-clean-less.md)

## Context

The project that keeps and fills the documents its users upload reported
four gaps against 2.7 (user report U7), in what
[0175](0175-bound-page-content-check-an-acts-upload-once-and-read-a-signed-body-last.md)
and [0176](0176-bound-what-a-pdf-decodes-list-placeholders-fill-drafts-and-clean-less.md)
built. Each has a workaround in the project.

1. **A body limit only as declared.** `upload_route` decides `max_size`
   per request, but `body_limit`, which a size so decided requires, only
   when the route is declared. A bound that is a setting is read twice:
   at import for the body, at each request for the file. A test that
   overrides the setting changes the file's bound and not the body's.
   The project read its settings when it declared its routes, and held
   its legal documents to the setting's ceiling.
2. **No dependency runs before the rules.** A route's `dependencies` run
   after its `Require` (`declaring.add_route`), so a limit cannot count a
   request before a rule that reads its body. `DeviceSigned` reads it
   once the request names a device of the caller's, where
   `DEVICES.CONTENT_DIGEST` is `"optional"` and the request sends no
   digest; a stolen session lists the caller's devices (`GET /devices`).
   The project wrote a rule that counts and allows, and put it first.
3. **`placeholders` and `fill_docx` parse what `ArchiveLimits` lets
   inflate, in the caller's process.** 64 MiB a part and 256 MiB in all,
   in the web worker. The project measured an XML part at about 17
   times its size once parsed, and a crafted file of 5.5 MiB that
   `OfficeCheck` took at 1.2 GiB in `placeholders` and 1.5 GiB in each
   preview. It checks its own limits (8 MiB a part, 24 MiB in all) in
   the check's process at upload, and passes them to every call.
4. **The service gets the stored file only.** `upload_route` stores the
   file, then drops what it received: a service that reads what the file
   holds (a PDF's pages, a template's placeholders) reads the storage
   again, in its transaction. The project's service does.

### What we measured

On 2.7.0, each call in a process of its own, its peak resident memory
over the process's before the call:

| File | `OfficeCheck` | `placeholders` |
| --- | --- | --- |
| 340 KB: 16 MiB of `<w:p/>` | took it, 8.7 s | 546 MiB, 4.2 s |
| 4 MiB: four parts of 48 MiB of `<w:p/>` | refused, too slow | 1.78 GiB, 49 s |
| 170 KB: 8 MiB of `<p/>x` (a default namespace) | | 529 MiB |
| 170 KB: 8 MiB of `<t/>x` in one paragraph | | 719 MiB |
| 4.6 MiB of Word XML written by pandoc, 3.4 MiB by LibreOffice | | 62 MiB, 39 MiB |

lxml keeps every element, text, and attribute as a node of libxml2,
about 120 bytes, an attribute's value as a text node below it, and
`fill_docx` keeps a Python object for each paragraph and text it reads:
120 to 250 bytes a node in all. Word's XML spends some 6 bytes a node, so
it costs about 12 times its size; XML written to cost spends 2.5, and
costs up to 90 times. The project's 17 was its file's: within its own
limits (24 MiB in all), the densest XML would take about 2 GiB. A
paragraph's placeholders cost as much again: each match is an object,
and 500,000 of them in 3 MB of text took 100 MiB.

### What the sources say

- **XML parsers bound documents by size and by count.** OWASP's XML
  Security Cheat Sheet asks to "enforce document-size, nesting-depth,
  and element-count limits when receiving and parsing XML", and says a
  large document "can exhaust server resources through buffering,
  parsing, validation, or application processing" [1]. Woodstox has both:
  `P_MAX_CHARACTERS`, the document's length, and `P_MAX_ELEMENT_COUNT`,
  its elements, unlimited by default [2]. .NET's
  `XmlReaderSettings.MaxCharactersInDocument` bounds the size, against
  "extremely large XML documents", 0 (none) by default [3]. lxml's
  parser, without `huge_tree`, bounds a node's depth (256) and a text's
  length, not their number [4]. Unbounded allocation is CWE-770 [5], and
  OWASP API4 asks that every payload be bounded [6].
- **A per-request body limit is set before the body is read.** ASP.NET
  Core's `IHttpMaxRequestBodySizeFeature.MaxRequestBodySize` is "the
  maximum allowed size of the current request body", which "cannot be
  modified after the reading the request body has started", and
  defaults to the server's limit [7]. Django checks
  `DATA_UPLOAD_MAX_MEMORY_SIZE` "when accessing `request.body` or
  `request.POST`" [8], reading the setting then.
- **FastAPI runs a route's `dependencies` in order, before the
  endpoint's own**, each awaited in turn: `APIRoute` puts them first in
  its dependant, in the order given [9]. So dependencies listed before
  the `Require` of the rules run before it.

### Options

How a body limit is decided per request:

| Option | What it costs |
| --- | --- |
| A second option, the ceiling, beside a per-request `body_limit` | Two knobs for one bound; most routes need one. |
| **`body_limit` a callable of the request and the aggregate, as `max_size` is; until it is decided, the body is held as a command route's without one (the resource's `body_limits["command"]`, or `MAX_REQUEST_BODY_SIZE`)** | What reads the body before the route decides (`DeviceSigned`, `IdempotencyMiddleware`) is held to that, as ASP.NET holds it to the server's [7]: a route with such a rule keeps a number. Before routing, a declared length is checked against 1 GiB, the most a route may decide, once one route decides. |
| A body read before the decision held to 1 GiB | A stolen session makes `DeviceSigned` read a gigabyte into memory. |

How dependencies run before the rules, and on the twin:

| Option | What it costs |
| --- | --- |
| `dependencies` before the rules | Breaks 2.7's routes: a limit there would count the requests the rules refuse. |
| **`before_rules=`, on `command_route` and `upload_route`, run before `Require`, on the act and on its twin** | The twin runs the rules, so a rule there reads the twin's body too: a limit that skipped the twin would leave an uncounted way in. A limit of acts alone skips the twin with `asking_authorization`, as the rules and `provide` do. |
| `before_rules=` on the act alone | A thief sends to the twin. |

How the XML parsed is bounded:

| Option | What it costs |
| --- | --- |
| Lower `max_part_bytes` and `max_total_bytes` (the project's 8 and 24 MiB) | Refuses large images, which are never parsed; still 2 GiB for the densest XML. |
| Run both in a process of their own, as the upload checks run | A process per preview, a pool or its start's cost, an async API in place of a function. |
| Count elements while parsing, with lxml's events | An event and an object per element; attributes and texts not counted. |
| **Count before parsing: the parts' bytes as the archive states them, against `max_xml_bytes`; the nodes their bytes can make (each `<` twice, each `=` once), against `max_nodes`; each paragraph's `{{` too, before its placeholders are read** | One pass of `bytes.count` a part. It counts more than lxml makes (an end tag, an `=` in text), never less: with no DTD and no entity, nothing else makes a node, and a character reference makes a brace only in the text, where it is counted. |

What the bounds default to:

| Option | What it costs |
| --- | --- |
| None: opt in | The default keeps taking the report's file, at 1.2 GiB a call. |
| **`max_xml_bytes` 8 MiB, `max_nodes` 500,000** | About 150 MiB at most for the XML, whatever it holds. A document of a few hundred pages of text passes; one past them, which 2.7 filled, is refused: a security fix, in the changelog's `Security` and in the upgrading notes. |
| 1,000,000 nodes | 250 MiB at most. |

How the service reads the file received:

| Option | What it costs |
| --- | --- |
| The file in the command | A command is a message: it is logged, bound by an act, kept by an outbox. A file handle is none of these. |
| **A `received` parameter the service names, read from its signature, given the `ReceivedUpload` open at its start; the route closes it however the service ends** | `ReceivedUpload.store(keep=True)` leaves the file open after storing. A service without the parameter is called as before. A task the service leaves running finds the file closed. |

## Decision

- **`upload_route(body_limit=...)`** takes a callable of the request and
  the aggregate reached (None on the collection), sync or async, decided
  with the other bounds once the reach holds, before the body is read,
  on the act and on its twin. A whole number of bytes from 1 to 1 GiB
  (`ValueError` otherwise: a 500, the body unread). The route holds the
  body to it from then on (`hold_body`, the request's scope):
  `RequestBodyLimitMiddleware` refuses a declared length over it before
  a byte is read, and a streamed body as soon as it passes it. The file
  is held to it too, where it is less than `max_size`: the file never
  exceeds the body limit, whatever read the body before. Until it is
  decided, the body is held as a command route's without `body_limit`.
  A route that decides marks its endpoint (`decides_body_limit`): before
  routing, a declared length is then checked against 1 GiB, and each
  route's own limit at its first read. A `max_size` decided per request
  still needs a `body_limit`, a number or decided.
- **`before_rules=`** on `command_route` and `upload_route` (and so
  `use_case`): FastAPI dependencies run before the route's `Require`, in
  order, after the resource's own and its tenant's. They run on the act
  and its twin. `dependencies` keep their place, after the rules, and
  stay the act's alone.
- **`ArchiveLimits(max_xml_bytes=8 MiB, max_nodes=500_000)`**: the
  content types and the parts filled, in all. A part's stated size is
  counted before it is inflated; its nodes, as above, before it is
  parsed; a paragraph's `{{` before its placeholders are read. Past
  either, `DocumentError` at once. Every other part is inflated a piece
  at a time to check its checksum, then only as `fill_docx` writes it
  back, one at a time; `placeholders` inflates none. Each part's tree is
  dropped before the next is parsed. Measured at the defaults: the
  densest attributes 123 MiB, `<t/>x` 109 MiB, 500,000 placeholders of
  distinct names 143 MiB in `placeholders`, 8 MiB of text 29 MiB; a 32
  MiB document holding a 54 MiB part, with its XML at the bounds, 145
  MiB in `fill_docx` (2.7 took 235 MiB, and had no bound on the XML).
- **A service that names `received`** is given the `ReceivedUpload`,
  stored with `keep=True`, its file sought to its start before each run
  (a retry's too), and closed in a `finally` once the service is done.
  A store that fails drops the file, as it did. The twin runs no service.

## Consequences

- A test that overrides a setting sees it at the next request, for the
  body and the file alike. A route whose rule reads the body
  (`DeviceSigned`), or that takes `Idempotency-Key`, keeps a number: the
  body is read before the route can decide.
- Once one route decides its limit, a request that never reads its body
  is no longer refused before routing for a length a route's limit would
  refuse; every body read is still held to its route's.
- `before_rules` count the requests the rules refuse, and a twin's asks:
  a limit there counts an ask and its act twice. A rule that decides who
  may act belongs in `rule`: a refusal before the rules answers before
  `401`. The project's counting rule can go, and so can a check of its
  own that reads `Content-Digest`, until `"required"` is set.
- A Word document whose XML passes 8 MiB, or 500,000 nodes and
  placeholders, is refused where 2.7 filled it: about 350 pages of
  LibreOffice's XML, fewer where formatting splits text into many runs.
  `ArchiveLimits(max_xml_bytes=..., max_nodes=...)` takes larger ones,
  and about 250 bytes of memory a node. The project's limits can go;
  checking at upload, in the check's process, stays a choice
  (docs/documents.md, "The XML parsed").
- `fill_docx` inflates each part not filled twice, to check it and to
  write it: a fifth more time than 2.7 for a document of images, and a
  fraction of the memory.
- Not bounded here: what a value adds where its placeholder is written.
  A value of a megabyte for 400,000 placeholders writes 400 GB; values
  are the caller's, and a project that fills user text into a template
  of another user's bounds the values.

## Sources

1. OWASP, XML Security Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/XML_Security_Cheat_Sheet.html>
2. Woodstox, "Configuring Woodstox III: Woodstox-specific properties": <https://github.com/FasterXML/woodstox/wiki/Configuring-Woodstox-III-%E2%80%90-Woodstox%E2%80%90Specific-Properties>
3. Microsoft, `XmlReaderSettings.MaxCharactersInDocument`: <https://learn.microsoft.com/en-us/dotnet/api/system.xml.xmlreadersettings.maxcharactersindocument>
4. lxml, "Parsing XML and HTML with lxml" (`huge_tree`): <https://lxml.de/parsing.html#parser-options>
5. CWE-770, Allocation of Resources Without Limits or Throttling: <https://cwe.mitre.org/data/definitions/770.html>
6. OWASP API Security Top 10 2023, API4 Unrestricted Resource
   Consumption: <https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption>
7. Microsoft, `IHttpMaxRequestBodySizeFeature.MaxRequestBodySize`: <https://learn.microsoft.com/en-us/dotnet/api/microsoft.aspnetcore.http.features.ihttpmaxrequestbodysizefeature.maxrequestbodysize>
8. Django, `DATA_UPLOAD_MAX_MEMORY_SIZE`: <https://docs.djangoproject.com/en/5.2/ref/settings/#data-upload-max-memory-size>
9. FastAPI, `fastapi/routing.py` (`APIRoute`, the route's dependencies
   inserted first, in order): <https://github.com/fastapi/fastapi/blob/master/fastapi/routing.py>
