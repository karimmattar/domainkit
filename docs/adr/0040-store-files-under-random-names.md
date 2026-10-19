# 0040. Store files under random names; trust only their content

- Status: Accepted
- Date: 2026-10-19

## Context

Services receive documents, photos, and imports. domainkit had no place
to keep files and no way to receive them. Django's storage API is
`save(name, content)`, with the name the caller chooses (usually the
client's file name), made unique by adding characters; its uploads trust
the client's content type unless a project checks it.

The ways in through a file upload are well known: a file name with `../`
or a drive letter that escapes the upload directory, a symbolic link inside
it that a later write follows, a "picture" that is really HTML or SVG with
a script, a name that overwrites another file or a server configuration
(`.htaccess`), a file too large for the disk, and a reader that sees a file
half written. Serving files has its own: under ADR 0021, every route says
who may call it, and a mounted `StaticFiles` is refused unless marked
public.

## Decision

- **A storage port**, `domainkit.storage.Storage`: `save`, `open`,
  `delete`, `exists`, `size`, `url`, with `FileSystemStorage` and
  `MemoryStorage`, configured in `STORAGES` with Django's `BACKEND` and
  `OPTIONS`. The port checks every name; backends only move bytes.
- **The storage chooses names.** `save` takes the content, a directory,
  and an extension, and returns a name of 128 random bits. Directories,
  names, and extensions are plain segments; any other name reads as
  missing. Nothing a client sends becomes part of a path, so there is no
  traversal to filter, no collision to resolve, and no name to guess.
- **On the file system,** paths are walked from the root one directory at
  a time with `O_NOFOLLOW`, files are opened the same way (and
  non-blocking, so a FIFO cannot hang a read), and only regular files are
  files. Writes go to a hidden temporary file that is synced and renamed
  into place. Files are `0600` and directories `0700` by default.
- **Types come from content.** `save_upload` reads the first 512 bytes and
  accepts only the types the route lists (`FileType`: a content type, an
  extension, a signature check). The client's `Content-Type` and file
  name decide nothing; the name is cleaned and returned to show people.
  SVG is not offered, since it can carry scripts.
- **Sizes are enforced while streaming**, by the storage itself
  (`max_size`), so nothing larger is kept; a file already known to be too
  large is refused before it is copied.
- **Serving goes through routes.** `serve` streams a stored file from a
  route that has a rule, as a download unless the type is an image or a
  PDF, with `Content-Security-Policy: sandbox` and `nosniff`. A storage
  served by a web server (`base_url`) or a mounted `StaticFiles` is public,
  and must be declared so (`mark_public`).
- `python-multipart` is an optional extra, `uploads`; nothing else is
  added.

## Consequences

A project cannot store a file under a name of its choosing; it keeps the
storage's name (and the client's, cleaned, for display) in its own tables,
which is where access checks need them anyway. Files are always served by
the application or by a server the project configures on purpose, never
by accident. `FileSystemStorage` needs POSIX `O_NOFOLLOW` and `dir_fd`, so
it does not run on Windows. Type detection by signature cannot tell CSV
from text or a `.docx` from a zip, and covers only the types listed;
projects describe others with `FileType`. Uploads are still bounded by
`MAX_REQUEST_BODY_SIZE` for the whole request, which projects accepting
large files must raise, globally.
