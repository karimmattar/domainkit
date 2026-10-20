"""Mail templates: a subject, a plain-text body, and an HTML one, from files.

A template named ``auth/password_reset`` is up to three files under
``templates/mail/`` in a domain package, or in a ``MAIL.TEMPLATE_DIRS``
directory, which are searched first; then the installed domains, in
``INSTALLED_DOMAINS`` order. The first that has the text body wins::

    auth/password_reset.subject.txt     one line
    auth/password_reset.txt             the plain-text body
    auth/password_reset.html            the HTML body (optional)

``{{ name }}`` and ``{{ user.email }}`` take values from the context: keys
of mappings, and attributes of other objects, never names that start with
``_`` nor methods. Values are escaped in HTML and left as they are in
text. A name the context lacks is an error, so no message goes out with a
hole in it. There are no loops or conditions: compute such text in Python.
"""

import html
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final

from domainkit.apps import registry

from .delivery import mail_settings
from .message import EmailMessage

TEMPLATE_DIR: Final = Path("templates", "mail")
"""Where a domain package keeps its mail templates."""
_NAME: Final = re.compile(r"[a-z0-9_-]+(?:/[a-z0-9_-]+)*")
_VARIABLE: Final = re.compile(
    r"\{\{\s*([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*)\s*\}\}"
)


class TemplateError(Exception):
    """A template cannot be rendered: bad syntax, or a value it lacks."""


class TemplateDoesNotExist(TemplateError):
    """No directory holds the template."""


def template_dirs() -> Iterator[Path]:
    """Directories searched for templates, first to last."""
    yield from mail_settings().TEMPLATE_DIRS
    for config in registry:
        yield config.path / TEMPLATE_DIR


def find_template(name: str) -> Path:
    """The directory that holds ``name``'s text body, joined with ``name``.

    Raises:
        TemplateError: ``name`` is not lower-case words separated by ``/``.
        TemplateDoesNotExist: No directory holds it.
    """
    if not _NAME.fullmatch(name):
        raise TemplateError(
            f"Template names are lower-case words separated by '/': {name!r}."
        )
    for directory in template_dirs():
        if (directory / f"{name}.txt").is_file():
            return directory / name
    raise TemplateDoesNotExist(f"No mail template {name!r} (looked for {name}.txt).")


def render_string(
    template: str, context: Mapping[str, object], *, escape: bool = False
) -> str:
    """``template`` with each ``{{ name }}`` replaced by its value, ``escape``d.

    Raises:
        TemplateError: Braces that are not a variable, or a name without a value.
    """
    parts = _VARIABLE.split(template)
    for text in parts[::2]:
        if "{{" in text or "}}" in text:
            raise TemplateError(
                "Template braces hold a name such as {{ user.email }}; names "
                "start with a letter."
            )
    rendered = []
    for index, part in enumerate(parts):
        if index % 2 == 0:
            rendered.append(part)
            continue
        value = str(_lookup(context, part))
        rendered.append(html.escape(value) if escape else value)
    return "".join(rendered)


def render_mail(
    template: str,
    context: Mapping[str, object],
    *,
    to: Sequence[str],
    from_email: str | None = None,
    reply_to: Sequence[str] = (),
) -> EmailMessage:
    """A message from the template ``template``, for ``to``.

    The subject is the subject file's text on one line; the HTML body is
    left out when the template has none.

    Raises:
        TemplateError: The template is missing, malformed, or lacks a value.
    """
    base = find_template(template)
    subject_file = base.with_name(f"{base.name}.subject.txt")
    html_file = base.with_name(f"{base.name}.html")
    subject = (
        render_string(_read(subject_file), context) if subject_file.is_file() else ""
    )
    return EmailMessage(
        subject=" ".join(subject.split()),
        body=render_string(_read(base.with_name(f"{base.name}.txt")), context),
        html=(
            render_string(_read(html_file), context, escape=True)
            if html_file.is_file()
            else None
        ),
        to=tuple(to),
        from_email=from_email,
        reply_to=tuple(reply_to),
    )


def _read(path: Path) -> str:
    """A template file's text."""
    return path.read_text(encoding="utf-8")


def _lookup(context: Mapping[str, object], name: str) -> object:
    """The value ``name`` (``user.email``) has in ``context``.

    Raises:
        TemplateError: Some part of it is missing, or is a method.
    """
    value: object = context
    for part in name.split("."):
        if isinstance(value, Mapping):
            if part not in value:
                raise TemplateError(f"The template needs {name!r}, which is missing.")
            value = value[part]
        else:
            try:
                value = getattr(value, part)
            except AttributeError:
                raise TemplateError(
                    f"The template needs {name!r}, which is missing."
                ) from None
        if callable(value):
            raise TemplateError(f"{name!r} is a method; pass its result instead.")
    return value
