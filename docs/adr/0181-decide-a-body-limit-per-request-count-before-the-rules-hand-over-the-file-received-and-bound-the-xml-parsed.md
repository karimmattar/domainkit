# 0181. Decide a body limit per request, count before the rules, hand over the file received, and bound the XML parsed

- Status: Accepted; amended 2026-11-18 and 2026-11-23 (at the end)
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

A review of the first cut of this change found two more ways past it.
`fill_docx` wrote each value as many times as the template named it:
a 50 KB template of 480,000 `{{a}}`, which `OfficeCheck` and
`placeholders` take, filled with a four-line address, wrote 16 MB of
text and 1.4 million elements, and took 1,075 MiB and 9 s. And lxml
keeps every element and attribute name a thread parses in a dictionary
of the thread's, for the thread's life [10]: a thread of a pool that
read documents of 250,000 distinct names each grew from 78 to 787 MiB
over 80 of them, where a thread per call stayed near 130 MiB.

Real documents, LibreOffice's and pandoc's of 311 to 483 pages: plain
text makes 140 to 330 nodes and 4 to 6 KB a page; formatted text (bold,
italics, links, tables, lists) 3,000 to 3,700 nodes and 17 to 23 KB a
page, at 70 to 100 bytes of memory a node.

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
| **`body_limit` a callable of the request and the aggregate, as `max_size` is; until it is decided, the body is held as a command route's without one (the resource's `body_limits["command"]`, or `MAX_REQUEST_BODY_SIZE`)** | What reads the body before the route decides (`IdempotencyMiddleware`, a dependency) is held to that, as ASP.NET holds it to the server's [7]. A rule that reads it (`DeviceSigned`) would always be: a rule says so (`reads_body`), and the route is refused when declared. Before routing, a declared length is checked against 1 GiB, the most a route may decide, once one route decides. |
| A body read before the decision held to 1 GiB | A stolen session makes `DeviceSigned` read a gigabyte into memory. |

How dependencies run before the rules, and on the twin:

| Option | What it costs |
| --- | --- |
| `dependencies` before the rules | Breaks 2.7's routes: a limit there would count the requests the rules refuse. |
| **`before_rules=`, on `command_route` and `upload_route`, run before `Require`, on the act and on its twin** | The twin runs the rules, so a rule there reads the twin's body too: a limit that skipped the twin would leave an uncounted way in. A limit of acts alone skips the twin with `asking_authorization`, as the rules and `provide` do. A command route's body is read by FastAPI before any dependency, so there they count before the rules only. |
| `before_rules=` on the act alone | A thief sends to the twin. |

How the XML parsed is bounded:

| Option | What it costs |
| --- | --- |
| Lower `max_part_bytes` and `max_total_bytes` (the project's 8 and 24 MiB) | Refuses large images, which are never parsed; still 2 GiB for the densest XML. |
| Run both in a process of their own, as the upload checks run | A process per preview, a pool or its start's cost, an async API in place of a function. |
| Count elements while parsing, with lxml's events | An event and an object per element; attributes and texts not counted. |
| **Count before parsing: the parts' bytes as the archive states them, against `max_xml_bytes`; the nodes their bytes can make (each `<` twice, each `=` once), against `max_nodes`; each paragraph's `{{` too, before its placeholders are read** | One pass of `bytes.count` a part. It counts more than lxml makes (an end tag, an `=` in text), never less: with no DTD and no entity, nothing else makes a node, and a character reference makes a brace only in the text, where it is counted. |
| **And what `fill_docx` writes: each value's bytes in UTF-8 against `max_xml_bytes`, and three nodes for each of its line breaks and tabs (a break, the text after it, and that text's `xml:space`) against `max_nodes`, before its paragraph is written** | A value counts each time the template names it. At two nodes a line, a value of 499,000 lines took 375 MiB. |

How lxml's dictionary of names is kept from growing:

| Option | What it costs |
| --- | --- |
| A parser option | lxml has none: each parser of a thread takes the thread's dictionary. |
| A process per call | A process start per preview, and an async API. |
| **A thread per call, which ends with it** | About 0.4 ms a call (a small template took 0.7 ms, 0.35 in the caller's thread). |

What the bounds default to:

| Option | What it costs |
| --- | --- |
| None: opt in | The default keeps taking the report's file, at 1.2 GiB a call. |
| `max_xml_bytes` 8 MiB, `max_nodes` 500,000 | About 200 MiB at most; refuses formatted text from 135 to 165 pages. |
| **`max_xml_bytes` 12 MiB, `max_nodes` 1,000,000** | 340 MiB at most, measured on XML written to cost the most. Formatted text passes up to some 270 to 330 pages, plain text some 2,100 to 2,900; one past them, which 2.7 filled, is refused: a security fix, in the changelog's `Security` and in the upgrading notes. |

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
  a byte is read, and a streamed body as soon as it passes it. A route's
  limit is fixed by the first message of its body. A probe for a
  disconnect (`Request.is_disconnected`, a `receive()` in a cancelled
  scope) made before the route decides is cancelled before it reaches
  the server, which keeps its message for the read: uvicorn hands back
  a message it holds without suspending, which the probe would take
  and Starlette drop. The file
  is held to it too, where it is less than `max_size`: the file never
  exceeds the body limit, whatever read the body before. Until it is
  decided, the body is held as a command route's without `body_limit`.
  A route whose rules hold one that reads the body (`BasePermission.reads_body`,
  set by `DeviceSigned`; `&`, `|` and `~` pass it on) is refused when it
  is declared (`ImproperlyConfigured`). A route that decides marks its
  endpoint (`decides_body_limit`): before routing, a declared length is
  then checked against 1 GiB, and each route's own limit at its first
  read. A `max_size` decided per request still needs a `body_limit`, a
  number or decided.
- **`before_rules=`** on `command_route` and `upload_route` (and so
  `use_case`): FastAPI dependencies run before the route's `Require`, in
  order, after the resource's own and its tenant's. They run on the act
  and its twin. `dependencies` keep their place, after the rules, and
  stay the act's alone.
- **`ArchiveLimits(max_xml_bytes=12 MiB, max_nodes=1_000_000)`**: the
  content types and the parts filled, in all, and what `fill_docx`
  writes in them. A part's stated size is counted before it is
  inflated; its nodes, as above, before it is parsed; a paragraph's `{{`
  before its placeholders are read; each value written, its bytes and
  three nodes a line break or tab, before its paragraph is written. Past
  either, `DocumentError` at once. Every other part is inflated a piece
  at a time to check its checksum, then only as `fill_docx` writes it
  back, one at a time; `placeholders` inflates none. Each part's tree is
  dropped before the next is parsed.
- **Each call of `placeholders` and `fill_docx` runs in a thread of its
  own**, which ends with it, its failure raised again in the caller: the
  names lxml keeps for the thread go with it. Over 40 calls of 250,000
  distinct names each, one calling thread stayed at 79 MiB, where the
  first cut grew to 434 MiB.
- Measured at the defaults, each call in a process of its own, its peak
  over the process's before: 340 MiB for one element of a million
  attributes of distinct names, 300 MiB for attributes spread over
  elements, 284 MiB for a million placeholders of distinct names, 218
  MiB for one paragraph of `<w:t/>x`, 250 MiB for `fill_docx` writing a
  value of 333,000 lines, 200 MiB for one refused as it wrote; a
  formatted document of LibreOffice's at the bound, 66 MiB.
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
- A Word document whose XML passes 12 MiB, or 1,000,000 nodes and
  placeholders, is refused where 2.7 filled it: formatted text from
  some 270 to 330 pages, plain text from some 2,100 to 2,900.
  `ArchiveLimits(max_xml_bytes=..., max_nodes=...)` takes larger ones,
  and up to 340 bytes of memory a node. The project's limits can go;
  checking at upload, in the check's process, stays a choice
  (docs/documents.md, "The XML parsed"). A template that names a value
  many times is refused when the value is long, or of many lines.
- `fill_docx` inflates each part not filled twice, to check it and to
  write it: a fifth more time than 2.7 for a document of images, and a
  fraction of the memory. Each call starts a thread: about 0.4 ms.
- The bounds hold one call, not a worker: `asyncio.to_thread` runs up
  to `min(32, cpus + 4)` calls at once, some 11 GiB at the bounds.
  domainkit adds no limiter; the docs show one the calls share
  (`anyio.CapacityLimiter`, one per event loop), which a project sizes
  for its workers' memory.
- pandoc writes a list's numbering part that inflates some 106 times,
  past `max_ratio`'s 100: such a document was refused in 2.7 too.

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
   inserted first, in order; the request handler reads the body before
   it solves them): <https://github.com/fastapi/fastapi/blob/master/fastapi/routing.py>
10. lxml 6.1.3, `debug.pxi` (`dict_size`: "the global name dictionary
    used by libxml2 for the current thread. Each thread has its own
    dictionary."): <https://github.com/lxml/lxml/blob/lxml-6.1.3/src/lxml/debug.pxi>

## Amendment (2026-11-18): the SAML ACS parses as Word documents are parsed

The 2.8 audit found the SAML ACS parsing what anyone may post with lxml
on the event loop's thread (A28-PERF-03): each response of new names
grew the thread's dictionary for good, and the loop waited while it
parsed. The ACS now takes this record's design:

- The response's XML is bounded before it is parsed: 50,000 nodes,
  counted on its bytes as above; no DTD or entity; read as UTF-8 whatever
  it declares, so the count sees what is parsed. A response not in UTF-8
  is refused.
- It is parsed in a thread of its own, which ends with it. At most 4 are
  parsed at once on an event loop, and a response that waits more than
  5 seconds for its turn is refused. The thread is not a daemon: a
  process that exits waits for a parse under way, which the bounds keep
  short.
- Counting nodes does not bound all of signxml's work before it checks
  the signature (the lead review of the fix). So the response is read
  once more in the thread before signxml runs: an element sits at most
  32 levels deep, with at most 32 namespace declarations on its path
  from the root, a prefix declared again counted again (Okta declares
  `xs` and `xsi` again on sibling values, so each path holds them once),
  each of a URI of 128 characters at most, which exclusive
  canonicalization writes again on each element that uses it, and at
  most 32 attributes, in the signed part signxml canonicalizes before it
  compares a digest as in a `SignedInfo`; a `SignedInfo` holds at most
  32 elements and 16 KiB (the re-checks of the fix). Within all the
  bounds, XML written to cost the most took up to 50 ms and 15 MiB when
  no one signed it. When the provider signed it and it was then
  changed, the costliest shapes took up to 275 ms, most of their runs
  150 to 230 ms, and up to 46 MiB over what the process held before,
  with inclusive or exclusive canonicalization, both readings and
  signxml's refusal included (measured at a load average near 8).

## Amendment (2026-11-23): a body limit decided before the rules

The project noted that a body limit decided per request is refused on a
route whose rule reads the body (user report U7). `DeviceSigned` reads
it to check what the device signed, even where `DEVICES.CONTENT_DIGEST`
is `"required"`. So a phone's upload took a fixed number, and a setting
used as the bound was read when the route was declared: a change took a
restart.

- **`upload_route(body_limit=BeforeRules(decide))`**, a new form of the
  body limit (`domainkit.resources.BeforeRules`). `decide` is given the
  request alone, sync or async, and returns 1 byte to 1 GiB
  (`ValueError` otherwise, a 500, the body unread). No rule has run when
  it decides, so it decides from the settings and what the request
  says (its path, its headers), not from who sends it.
- It runs first among the route's `before_rules`, after the resource's
  own dependencies and its tenant's, on the act and on its twin, and
  holds the body as a limit decided once the reach holds does
  (`hold_body`): a declared length over it is 413 before a byte is
  read, a streamed body as soon as it passes it, whatever reads it after,
  the rule included. The file is held to it too, where it is less than
  `max_size`. The route marks its endpoint (`decides_body_limit`), so a
  declared length is checked against 1 GiB before routing, as for the
  other form. What reads the body before it (`IdempotencyMiddleware`, a
  resource's own dependency) is held as on a command route without
  `body_limit`, and the file to the limit decided.
- A limit decided once the reach holds, on a route whose rule reads the
  body, is still refused when the route is declared, and the refusal
  names `BeforeRules`.
- Options weighed:

  | Option | What it costs |
  | --- | --- |
  | `hold_body` public, for a `before_rules` dependency of the project's, the report's second option | The route would not know its limit is decided: a declared length over its number is refused before routing, and the file is not held. The order would be the project's to keep: in `dependencies`, after the rules, it holds nothing the rule read, and nothing says so. |
  | A callable of the request alone, told from the other form by its signature | The call site does not show when it runs; a callable with a default parameter reads both ways. |
  | The callable of the request and the aggregate, run before the rules with no aggregate where a rule reads the body | An existing callable would be given `None` on a route on one aggregate. |
  | **`BeforeRules(decide)`, chosen** | One name more. The framework puts the decision first, and holds what reads the body after it, as ASP.NET's per-request limit must be set before the body is read [7]. |
