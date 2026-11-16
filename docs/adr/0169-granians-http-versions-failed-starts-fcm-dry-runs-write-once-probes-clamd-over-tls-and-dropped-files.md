# 0169. Granian's HTTP versions and failed starts, FCM's dry runs, write-once probes, clamd over TLS, and dropped files

- Status: Accepted
- Date: 2026-11-16

## Context

A project's third report on 2.4.0 named five gaps between what
domainkit says and what it does, each with a workaround in the project.

1. **`serve` (S1).** `manage.py serve` passes Granian no `http`, so
   Granian 2.8.4 speaks `auto`: HTTP/1.1, and HTTP/2 without TLS (h2c)
   to a client that opens with HTTP/2's preface (`server/common.py`,
   `http: HTTPModes = HTTPModes.auto`) [1, 2]. The image said HTTP/1.1
   under uvicorn, which speaks nothing else. Over HTTP/2, Granian's own
   limit on a request's headers is 16 MiB (`HTTP2Settings.max_headers_size`),
   so `SERVE["MAX_HEADER_SIZE"]` held for HTTP/1.1 only. And `serve`
   always sets `respawn_failed_workers`: Granian starts a dead worker
   again unless it dies within 5.5 seconds of being started again
   (`_serve_loop`), so a startup that takes longer to fail, such as one
   waiting for a database that is not there, is started again for ever,
   and the container stays up, serving nothing.
   - uvicorn 0.54's supervisor stops when a worker exits with
     `STARTUP_FAILURE` (3): "the app, TLS or socket bind is broken and
     would fail the same way on every restart" [3]; with one worker,
     uvicorn exits 3. gunicorn halts with "Worker failed to boot." and
     exits 3 (`WORKER_BOOT_ERROR`) [4].
   - Granian's main process never sees whether a worker served: its
     lifespan worker calls `sys.exit(1)` when the application's startup
     fails, as when a served worker crashes. `serve(target_loader=...)`
     is its documented way to load the target in each worker.
2. **FCM (N1).** 2.4 took FCM's `INVALID_ARGUMENT` with its own error
   and no field for a token gone. FCM answers a message too big
   ("Android message is too big") the same way, so one large message
   forgot a working token. Firebase's guidance: `INVALID_ARGUMENT` means
   an invalid token "only if the payload is completely valid" [5]; its
   reference lists message size, reserved keys, and the TTL among the
   causes [6]. And `refusal` assumed `error` an object and `details` a
   list: `{"error": {"details": 5}}` raised `TypeError` in `send`.
3. **Write once (U4).** A presigned upload's URL signs
   `If-None-Match: *`, which is what keeps the file the application
   checked from being replaced through the same URL. AWS S3 refuses a
   second such write with `412` since 2024 [7], and Cloudflare R2 does
   [8]. SeaweedFS, RustFS, and Versity's gateway read the header on
   `PutObject` in their source [9, 10, 11]. Garage 2.5.0 reads it for
   reads only (`get.rs`), never in `put.rs` [12], and Scality
   CloudServer 9.5.6's `objectPut.js` reads no condition [13]: there the
   second write replaces the file, without an error. The module's
   docstring named MinIO first, whose repository is "no longer
   maintained" and whose community edition is "distributed as source
   code only" [14].
4. **clamd (U5).** clamd speaks no TLS and asks no one who they are;
   ClamAV's own advice for its TCP socket is to protect the interfaces
   it listens on [15]. On another host, `ClamAVScanner` sends every
   upload in clear. stunnel ends TLS in front of a plain TCP service and
   can require a client certificate (`verifyChain`) [16].
5. **Dropped files (U5).** Nothing removed a stored file once the change
   that replaced or deleted it committed, nor checked that no other row
   held its name. django-cleanup deletes a replaced or deleted file in
   the commit phase of the transaction, and assumes one reference per
   file [17]; Active Storage purges a blob after the attachment's
   `after_destroy_commit`, in a job, and a foreign key keeps a blob
   another record holds [18].

Options considered:

- **HTTP.** (a) Default `"1"`: the image's old behaviour, and the safer
  one; but a project whose proxy speaks h2c to `serve` since 2.4 would
  stop being answered on upgrade. (b) **A setting defaulting to
  `"auto"`, as 2.4**, documented with when to choose `"1"`. Either way
  `MAX_HEADER_SIZE` should bound HTTP/2's headers too.
- **Failed starts.** (a) `respawn_failed_workers=False`: any dead worker
  stops the server, one that served included. (b) A time window: as
  arbitrary as Granian's 5.5 seconds. (c) **The worker says so**: it
  loads the application through `serve`, which signals the main process
  when the import fails or the startup sends `lifespan.startup.failed`;
  the main process stops Granian as `SIGTERM` does, and `serve` exits 3.
  A dead worker that served is started again. Opt in, as a project might
  rely on a start retried until its database comes up.
- **FCM.** (a) Read FCM's English sentence for an invalid token, as the
  project does: it may change without notice. (b) Count a fieldless
  `INVALID_ARGUMENT` as gone only in `check_token`: a token that FCM no
  longer takes would then be kept, refused for good each time. (c)
  **Ask a dry run of the token alone** when `send` gets one: the payload
  is then valid, which is the case Firebase's guidance names.
- **Write once.** (a) In `check --deploy`: `check` touches no network
  today, and runs in pipelines that may not reach the bucket. (b) Before
  every first presigned upload, by default: `presigned_upload` sends no
  request today, so projects' tests and stubs would see new requests,
  and a store that ignores the header would stop presigned uploads on
  upgrade. (c) **A public probe, and an opt-in option that runs it
  before the first presigned upload and fails closed.**
- **clamd.** (a) An error where deployed, as for a broker without TLS:
  the project's clamd has no port outside its stack's network, which
  only the service reaches. (b) **TLS options on the scanner, and a
  warning** where deployed when clamd is on another host without TLS,
  which such a project silences.
- **Dropped files.** (a) ORM events that guess which files a change
  dropped, as django-cleanup does: implicit, and the domain changes its
  aggregates, not rows. (b) **A deferred task the use case queues**,
  which checks the declared columns on the primary before it deletes.

## Decision

- `SERVE["HTTP"]` (`"auto"`, `"1"`, `"2"`, default `"auto"`) is
  Granian's `--http`. `"2"` is refused under uvicorn, which speaks
  HTTP/1.1 only, and with `WEBSOCKETS` on, which Granian cannot carry
  over HTTP/2. `serve` gives Granian `MAX_HEADER_SIZE` as
  `--http2-max-headers-size` too.
- `SERVE["EXIT_ON_FAILED_START"]` (default `False`): under Granian, each
  worker loads `ASGI_APPLICATION` through `serve.load_application`,
  which sends `SIGUSR1` to the main process when the import fails or
  the startup fails; the main process raises `SIGTERM` for Granian's own
  handler, and `serve` exits 3. Requests reach the application in one
  plain call. uvicorn already stops so.
- `FcmPushBackend.refusal` takes an `INVALID_ARGUMENT` naming no field
  for a lasting refusal of the message, and reads any shape of answer
  without raising. `send`, on such a refusal, runs `check_token`: a
  token the dry run refuses is gone; one it takes is kept, and the
  message refused for good; a dry run that cannot tell is tried again.
  `check_token` takes an `INVALID_ARGUMENT` naming no other field than
  the token for a token gone.
- `S3Storage.probe_write_once()` writes a probe object under the prefix
  twice with `If-None-Match: *`, expects `412` (or `409`), and deletes
  it. A store that took the second write raises `WriteOnceIgnored`, a
  `StorageError` and an `UpstreamError` the HTTP layer answers `503
  direct_upload_unavailable`; a probe that cannot tell raises a
  `StorageError` that says it was the probe.
  `S3Storage(check_write_once=True)` runs it before the first presigned
  upload of each storage, once however many uploads ask at once, and
  keeps a verdict; one that could not tell is asked again. The docstring
  and `uploads.md` name the stores that honour the header and those
  that do not.
- `ClamAVScanner(tls=True, tls_ca=..., tls_cert=..., tls_key=...)`
  speaks TLS 1.2 or later to a proxy in front of clamd, checking its
  certificate against `host`; an encrypted key needs `tls_key_password`,
  rather than a prompt on the terminal. `check` warns where deployed
  (`security.W007`) when `UPLOAD_SCANNER` reaches clamd, through
  `ClamAVScanner` or a subclass, on a host other than `localhost`,
  `127.0.0.1` or `::1` without `tls`.
- `domainkit.storage.cleanup`: `keeps_files(*columns, storage=...)`
  declares the columns that hold a storage's names: a table's own
  column, as the framework's layout makes tables with `table_for` and
  maps entities later (link tables included), or a mapped attribute.
  `remove_unreferenced(names, storage=...)` reads them on the primary of
  each table's domain, and deletes the names no row holds, passing over
  empty ones; without a declared column it refuses.
  `domainkit.contrib.messaging` registers it as the task
  `domainkit.storage.remove_unreferenced`, so a use case defers it with
  `uow.defer`, with a `delay` to narrow the window in which a request
  still copies the old name.

## Consequences

- A project keeps Granian to HTTP/1.1 with one key, and its header limit
  holds over HTTP/2. The default stays 2.4's; a later major version may
  make `"1"` the default.
- With `EXIT_ON_FAILED_START`, a container whose application cannot
  start exits 3 at the first failure, and its orchestrator restarts it
  or reports it, as under uvicorn and gunicorn. It needs POSIX signals,
  and a worker killed before its startup ends still sends nothing. A
  worker whose startup fails as Granian restarts it after it served, or
  on `SIGHUP`, stops the server too.
- A message FCM refuses as too big costs one more request, a dry run,
  and no longer a working phone. A subclass that reads the case itself,
  as the project does, still works, and may go.
- `check_write_once` costs three requests per storage and process before
  the first presigned upload, and needs `DeleteObject` on the prefix to
  leave nothing behind. Without it, as before, nothing is asked: a
  project on a store that is not known to honour the header turns it on.
- `security.W007` makes `check --deploy` fail where clamd is reached in
  clear on another host, until TLS is on or the warning is silenced.
- Removing a dropped file is opt-in and explicit: the use case defers it,
  and the infrastructure declares the columns. A transaction still open
  as the check reads, which copies the name into a row, is not seen; the
  docs say to copy the file instead of its name. Columns that hold
  several names in one value are not read.

## References

1. Granian 2.8.4, `granian/server/common.py` and `granian/http.py`:
   <https://github.com/emmett-framework/granian/blob/master/granian/server/common.py>
2. RFC 9113, HTTP/2, 3.3 "Starting HTTP/2 with Prior Knowledge":
   <https://www.rfc-editor.org/rfc/rfc9113#section-3.3>
3. uvicorn, `uvicorn/supervisors/multiprocess.py` (`STARTUP_FAILURE`):
   <https://github.com/encode/uvicorn/blob/master/uvicorn/supervisors/multiprocess.py>,
   and <https://github.com/encode/uvicorn/discussions/2440>
4. gunicorn, `gunicorn/arbiter.py` (`WORKER_BOOT_ERROR`):
   <https://github.com/benoitc/gunicorn/blob/master/gunicorn/arbiter.py>
5. Firebase, "Best practices for FCM registration token management",
   detecting invalid token responses:
   <https://firebase.google.com/docs/cloud-messaging/manage-tokens>
6. FCM HTTP v1, `ErrorCode`:
   <https://firebase.google.com/docs/reference/fcm/rest/v1/ErrorCode>
7. AWS, "How to prevent object overwrites with conditional writes":
   <https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html>
8. Cloudflare R2, S3 API compatibility (`PutObject` conditions):
   <https://developers.cloudflare.com/r2/platform/s3-compatibility/api/>
9. SeaweedFS, `weed/s3api/s3api_object_handlers_put.go`
   (`checkConditionalHeaders`):
   <https://github.com/seaweedfs/seaweedfs/blob/master/weed/s3api/s3api_object_handlers_put.go>
10. RustFS, `rustfs/src/app/object/put_input.rs`:
    <https://github.com/rustfs/rustfs/blob/main/rustfs/src/app/object/put_input.rs>
11. Versity S3 Gateway, `s3api/controllers/object-put.go`:
    <https://github.com/versity/versitygw/blob/main/s3api/controllers/object-put.go>
12. Garage 2.5.0, `src/api/s3/put.rs` and `src/api/s3/get.rs`:
    <https://github.com/deuxfleurs-org/garage/blob/main-v2/src/api/s3/put.rs>
13. Scality CloudServer 9.5.6, `lib/api/objectPut.js`:
    <https://github.com/scality/cloudserver/blob/development/9.5/lib/api/objectPut.js>
14. MinIO's README ("THIS REPOSITORY IS NO LONGER MAINTAINED",
    "Source-Only Distribution"):
    <https://github.com/minio/minio/blob/master/README.md>
15. ClamAV, "Regarding the Use of ClamAV Daemon's TCP Socket" (2016):
    <https://blog.clamav.net/2016/06/>
16. stunnel's manual (`accept`, `connect`, `CAfile`, `verifyChain`):
    <https://www.stunnel.org/docs.html>
17. django-cleanup's README:
    <https://github.com/un1t/django-cleanup>
18. Rails, `activestorage/app/models/active_storage/attachment.rb`:
    <https://github.com/rails/rails/blob/main/activestorage/app/models/active_storage/attachment.rb>
