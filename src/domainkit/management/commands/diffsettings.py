"""``diffsettings``: the settings that differ from the framework's defaults."""

import re
from argparse import ArgumentParser
from collections.abc import Mapping
from typing import Annotated, Any, Final, get_args, get_origin

from pydantic import BaseModel, SecretBytes, SecretStr
from pydantic_core import to_jsonable_python

from domainkit.apps import registry
from domainkit.conf import unwrap_settings
from domainkit.conf.global_settings import HIDDEN
from domainkit.management.base import BaseCommand

SECRET_KEY: Final = re.compile(
    r"pass|secret|token|key|credential|auth|(?<![a-z])(?:sid|pin|pwd)(?![a-z])",
    re.IGNORECASE,
)
"""Names of settings and entries shown hidden (``CONNECT_ARGS``, ``APP_SID``).

``sid``, ``pin``, and ``pwd`` (ODBC's ``PWD=``) only as words of their own.
"""
URL_USERINFO: Final = re.compile(
    r"""
    (?<!\S)  # A word, read from its start once.
    (?>(\S*?)(?<![a-z0-9+.-])([a-z0-9+.-]++://))  # Its first URL's scheme,
    (\S*)@  # and that URL's user and password, up to the word's last @.
    """,
    re.IGNORECASE | re.VERBOSE,
)
"""A URL's user and password, up to the last ``@`` of the address.

Each word is read from its start once, at its first URL, so a long text is
read in a time that grows with its length, as ``SECRET_PAIR`` reads it.
"""
SECRET_PAIR: Final = re.compile(
    rf"""
    (?<![\w.-])  # From the start of a name, but a host and port after // or @:
    (?!(?:(?<=//)|(?<=@))[\w.-]++:\d++(?![^/?#\s"',)\]}}>]))
    (["']?+(?=[\w.-]*?(?:{SECRET_KEY.pattern}))[\w.-]++["']?+\s*+(?:(=)|:)
    [^\S\r\n]*+(?:\r?\n[^\S\r\n]++)?+)  # the name, quoted or not; its value
    (  # on its line, or the next one indented: quoted, and read on after =,
        (?:"(?:[^"\\]|\\.)*+"|'(?:[^'\\]|\\.)*+')  # and after : unless a
        (?(2)[^\s&;#]*+|(?:(?![^\s&;#,)\]}}])|[^\s&;#]*+))  # delimiter follows;
        | (?:[a-z][\w-]*+[ \t]++(?=[\w.~+/-]++=*+(?![\w.~+/=-])(?!\s*+[:=])))?+
        [^\s&;#]*+  # or up to a space, &, ;, or #, a scheme first (Bearer x).
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
"""``password=...`` and ``password: ...``, the name quoted or not.

As in a URL's query, a libpq connection string (``password = x``), JSON
(``"private_key": "..."``), or a header (``Authorization: Bearer x``).
A value starts on the name's line, or on the next one indented. After
``=``, a quoted value is read on past its closing quote, to a space, ``&``,
``;``, or ``#``, so ``password="a",x`` is hidden whole; after ``:``, as in
JSON, it ends at its closing quote where a delimiter follows. A host that a
port follows, after ``//`` or ``@``, is no name, so
``https://auth.example.com:8443`` keeps its port. Each name and each value
is matched from its start once, never again from within it, so a long text
is read in a time that grows with its length.
"""
UNSHOWN: Final = frozenset({"SETTINGS_MODULE"})


class Command(BaseCommand):
    """Print ``NAME = value`` for each changed setting; ``--all`` for every one.

    A domain's settings (``AUTH``, ``APPROVALS``, ...) are shown as their
    schema validated them, defaults filled in. Secrets stay hidden: secret
    values (``SecretStr`` keys and tokens), settings or entries whose names
    suggest secrets (a project's ``PAYMENT_API_KEY``, ``AUTH.TOKEN_TTL``),
    the user and password of a URL, ``password=...`` and ``password: ...``
    pairs, and, in a domain's settings, every text that no type describes:
    the strings of a mapping, of a value typed ``Any`` (``SMS_OPTIONS``), or
    of a value that is not a model (a dataclass) show as hidden, beside
    their keys and numbers.
    """

    help = "Show settings that differ from the defaults (--all: every setting)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept ``--all``."""
        parser.add_argument(
            "--all",
            dest="every",
            action="store_true",
            help="show every setting; defaults are marked with ###",
        )

    def handle(self, *, every: bool, **options: object) -> None:
        """Print one line per setting, sorted by name."""
        current = unwrap_settings()
        # A domain's settings come from its schema (``_domain_settings``):
        # as written, they may hold what JSON cannot (bytes not UTF-8).
        domain = set(current.model_extra or {})
        values = current.model_dump(mode="json", fallback=_named, exclude=domain)
        defaults = (
            type(current).model_construct().model_dump(mode="json", fallback=_named)
        )
        domains = _domain_settings()
        values |= {name: value for name, (value, _) in domains.items()}
        defaults |= {name: default for name, (_, default) in domains.items()}
        self.write(f"# {current.SETTINGS_MODULE}: settings that differ from defaults")
        for name in sorted(set(values) - UNSHOWN):
            value = _scrub(values[name])
            unchanged = name in defaults and _scrub(defaults[name]) == value
            if SECRET_KEY.search(name) and name not in domains:
                value = HIDDEN
            if unchanged and not every:
                continue
            mark = "### " if unchanged else ""
            self.write(f"{mark}{name} = {value!r}")


def _domain_settings() -> dict[str, tuple[Any, Any]]:
    """Each installed domain's settings, validated, and its schema's defaults.

    By the name they are read from (``AUTH``), as ``_shown`` shows them.
    """
    return {
        config.settings_name: (
            _shown(registry.settings(schema)),
            _shown(schema.model_construct()),
        )
        for config in registry
        if (schema := config.settings_class) is not None
    }


def _shown(model: BaseModel) -> dict[str, Any]:
    """A domain's settings as JSON, failing closed on what no type describes.

    A field of a model is shown by its type: a model field by its own
    fields, a secret (``SecretStr``, bytes) hidden, and a field typed
    ``Any`` or a mapping as ``_opaque`` shows it. A field the model lacks
    (a default left out of ``model_construct``) is left out.
    """
    fields = type(model).model_fields
    return {
        name: _typed(getattr(model, name), _free_form(field.annotation))
        for name, field in fields.items()
        if hasattr(model, name)
    }


def _typed(value: object, free_form: bool) -> object:
    """``value`` of a typed field: shown, but secrets and untyped text hidden.

    A value of fields or entries that is not a model, such as a dataclass,
    or a mapping in a ``NamedTuple``, is shown as ``_opaque`` shows it: no
    model's types say what it holds.
    """
    if free_form:
        return _opaque(value)
    if isinstance(value, BaseModel):
        return _shown(value)
    if isinstance(value, SecretStr | SecretBytes | bytes | bytearray):
        return HIDDEN
    if isinstance(value, list | tuple | set | frozenset):
        return _items(value, [_typed(item, free_form=False) for item in value])
    shown = to_jsonable_python(value, fallback=_named)
    if isinstance(shown, dict | list):
        return _opaque(shown)
    return shown


def _opaque(value: object) -> object:
    """``value`` with its keys, numbers, and booleans shown, and its text hidden.

    No type says what a free-form value holds (an SMS provider's
    ``APP_SID``, a key in PEM or JSON, a webhook's address), so a string is
    shown hidden whatever its name. A model inside is shown by its type.
    """
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, BaseModel):
        return _shown(value)
    if isinstance(value, Mapping):
        return {str(key): _opaque(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return _items(value, [_opaque(item) for item in value])
    return HIDDEN


def _items(value: object, shown: list[object]) -> list[object]:
    """The ``shown`` items of ``value``, a set's sorted by their text.

    A set's order follows the hash seed, which differs between processes:
    sorted, two runs print the same lines.
    """
    return sorted(shown, key=repr) if isinstance(value, set | frozenset) else shown


def _free_form(annotation: object) -> bool:
    """Whether a field so typed holds values no type describes.

    ``Any``, ``object``, and a mapping, anywhere in the type:
    ``dict[str, Any]``, ``Mapping[str, str] | None``, ``list[dict]``.
    """
    if annotation is Any or annotation is object:
        return True
    origin = get_origin(annotation)
    if origin is Annotated:
        return _free_form(get_args(annotation)[0])
    container = origin if origin is not None else annotation
    if isinstance(container, type) and issubclass(container, Mapping):
        return True
    return any(_free_form(argument) for argument in get_args(annotation))


def _named(value: object) -> str:
    """A value JSON cannot hold, as text: a class or function by its dotted path.

    ``LOGGING`` holds classes (``"()": ContextFilter``), as
    ``build_logging_config`` and ``logging.config`` write them.
    """
    module = getattr(value, "__module__", None)
    name = getattr(value, "__qualname__", None)
    if module is not None and name is not None:
        return f"{module}.{name}"
    return repr(value)


def _scrub(value: object) -> object:
    """``value`` with secret-looking entries, URL users, and passwords hidden."""
    if isinstance(value, dict):
        return {
            key: HIDDEN if SECRET_KEY.search(str(key)) else _scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        value = URL_USERINFO.sub(_userinfo, value)
        return SECRET_PAIR.sub(rf"\g<1>{HIDDEN}", value)
    return value


def _userinfo(match: re.Match[str]) -> str:
    """A URL's address with its password hidden, and its user where unclear.

    ``postgresql://app:p@ss@db`` keeps ``app``, and hides up to the last
    ``@``; a user that holds a ``/``, or no ``:`` at all (a key in the
    user's place, as some services give), is hidden too.
    """
    scheme, userinfo = match[1] + match[2], match[3]
    user, colon, _ = userinfo.partition(":")
    if colon and "/" not in user:
        return f"{scheme}{user}:{HIDDEN}@"
    return f"{scheme}{HIDDEN}@"
