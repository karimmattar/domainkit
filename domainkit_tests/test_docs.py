"""The reference documentation names every setting and command."""

import ast
import re
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter
from pydantic.fields import FieldInfo

from domainkit.conf.env import project_root
from domainkit.conf.global_settings import (
    BrokerConfig,
    CacheConfig,
    DatabaseConfig,
    DebugToolbarConfig,
    HttpClientConfig,
    LedgerConfig,
    Settings,
    ShardConfig,
    StorageConfig,
)
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.contrib.auth.settings import (
    AuthSettings,
    ClientCertSettings,
    CookieSettings,
    JWTSettings,
    MfaSettings,
    PasswordValidatorConfig,
    PrincipalCacheSettings,
)
from domainkit.contrib.evidence.settings import EvidenceSettings
from domainkit.contrib.flags.settings import FlagsSettings
from domainkit.contrib.idempotency.settings import IdempotencySettings
from domainkit.contrib.mail.settings import MailSettings, SmtpSettings
from domainkit.contrib.messaging.settings import MessagingSettings
from domainkit.contrib.notifications.settings import NotificationsSettings
from domainkit.contrib.webhooks.settings import WebhookSettings
from domainkit.management import BUILTIN_COMMANDS, _find_commands

DOCS = Path(__file__).parents[1] / "docs"
CONTRIB_COMMANDS = (
    "domainkit.contrib.messaging.management.commands",
    "domainkit.contrib.audit.management.commands",
    "domainkit.contrib.evidence.management.commands",
    "domainkit.contrib.auth.management.commands",
    "domainkit.contrib.flags.management.commands",
    "domainkit.contrib.webhooks.management.commands",
    "domainkit.contrib.privacy.management.commands",
)


def documented(page: str) -> set[str]:
    """Return every word written in backticks on ``page``."""
    text = re.sub(r"```.*?```", "", (DOCS / page).read_text(), flags=re.DOTALL)
    return {word for code in re.findall(r"`([^`]+)`", text) for word in code.split()}


@pytest.mark.parametrize(
    ("model", "page"),
    [
        (Settings, "reference.md"),
        (DatabaseConfig, "reference.md"),
        (LedgerConfig, "reference.md"),
        (ShardConfig, "reference.md"),
        (CacheConfig, "reference.md"),
        (BrokerConfig, "reference.md"),
        (StorageConfig, "reference.md"),
        (DebugToolbarConfig, "reference.md"),
        (HttpClientConfig, "reference.md"),
        (MessagingSettings, "contrib.md"),
        (AuditSettings, "contrib.md"),
        (IdempotencySettings, "contrib.md"),
        (EvidenceSettings, "contrib.md"),
        (FlagsSettings, "contrib.md"),
        (WebhookSettings, "contrib.md"),
        (AuthSettings, "auth.md"),
        (JWTSettings, "auth.md"),
        (CookieSettings, "auth.md"),
        (MfaSettings, "auth.md"),
        (ClientCertSettings, "auth.md"),
        (PrincipalCacheSettings, "auth.md"),
        (MailSettings, "mail.md"),
        (SmtpSettings, "mail.md"),
        (NotificationsSettings, "mail.md"),
    ],
)
def test_every_setting_is_documented(model: type[BaseModel], page: str) -> None:
    """A new field needs a row in its table."""
    assert set(model.model_fields) - documented(page) == set()


def test_every_password_validator_key_is_documented() -> None:
    """``PASSWORD_VALIDATORS`` entries are shown as dicts, not as a table."""
    page = (DOCS / "auth.md").read_text(encoding="utf-8")

    assert [
        name for name in PasswordValidatorConfig.model_fields if f'"{name}"' not in page
    ] == []


def test_every_command_is_documented() -> None:
    """Built-in and contrib commands each have a row."""
    commands = set(_find_commands(import_module(BUILTIN_COMMANDS).__path__))
    for package in CONTRIB_COMMANDS:
        commands |= set(_find_commands(import_module(package).__path__))

    assert commands - documented("reference.md") == set()


ROOT = DOCS.parent
REPOSITORY = "https://github.com/karimmattar/domainkit/"
"""Absolute links to this repository (the README's, for PyPI) name local files."""
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_DEFINITION = re.compile(r"^\[[^\]]+\]:\s+(\S+)$", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^```.*?^```$", re.DOTALL | re.MULTILINE)


def pages() -> list[Path]:
    """Every page whose links are checked: docs/, the README, and its neighbours."""
    return [
        ROOT / "README.md",
        ROOT / "SECURITY.md",
        ROOT / "CHANGELOG.md",
        # The ADR template's links are placeholders.
        *sorted(set(DOCS.rglob("*.md")) - {DOCS / "adr" / "template.md"}),
    ]


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading: lower case, punctuation dropped."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[^\w\- ]", "", text.lower())
    return text.replace(" ", "-")


def anchors(page: Path) -> set[str]:
    """Every heading anchor of ``page``; repeated headings get ``-1``, ``-2``."""
    seen: dict[str, int] = {}
    found = set()
    for line in _FENCE.sub("", page.read_text(encoding="utf-8")).splitlines():
        if match := _HEADING.match(line):
            anchor = slug(match[1])
            count = seen.get(anchor, 0)
            found.add(anchor if count == 0 else f"{anchor}-{count}")
            seen[anchor] = count + 1
    return found


def links(page: Path) -> list[str]:
    """The targets of the page's links, outside code blocks and code spans."""
    text = re.sub(r"`[^`\n]*`", "", _FENCE.sub("", page.read_text(encoding="utf-8")))
    return _LINK.findall(text) + _DEFINITION.findall(text)


def broken(page: Path, target: str) -> str | None:
    """Why the link from ``page`` to ``target`` leads nowhere, or None."""
    if target.startswith(REPOSITORY):
        path, _, anchor = target.removeprefix(REPOSITORY).partition("#")
        kind, _, path = path.partition("/main/")
        if kind not in {"blob", "tree"}:
            return None  # Issues, compare views, releases: not files.
        file = ROOT / path
    elif re.match(r"^[a-z][a-z0-9+.-]*:", target):
        return None  # Other sites, and mailto:.
    else:
        path, _, anchor = target.partition("#")
        file = (page.parent / path).resolve() if path else page
    if not file.exists():
        return f"{target}: no such file"
    if anchor and file.suffix == ".md" and anchor not in anchors(file):
        return f"{target}: no such heading"
    return None


@pytest.mark.parametrize("page", pages(), ids=lambda page: page.name)
def test_every_link_leads_somewhere(page: Path) -> None:
    """Relative links, and links to this repository, name files and headings."""
    assert [
        problem for target in links(page) if (problem := broken(page, target))
    ] == []


def test_the_middleware_table_is_the_order_the_template_and_defaults_keep() -> None:
    """The template's and the default stacks are the table's, with rows left out."""
    from domainkit.conf.global_settings import DEFAULT_MIDDLEWARE

    reference = (DOCS / "reference.md").read_text(encoding="utf-8")
    table = section_of(reference, "## Middleware")
    order = re.findall(r"^\| `(domainkit\.[\w.]+Middleware)` \|", table, re.MULTILINE)
    template = (
        ROOT
        / "src/domainkit/management/templates/project/src/config/settings/base.py-tpl"
    ).read_text(encoding="utf-8")
    listed = r'^    "(domainkit\.[\w.]+Middleware)",'
    installed = re.findall(listed, template, re.MULTILINE)

    assert len(installed) == 11
    assert len(DEFAULT_MIDDLEWARE) == 8
    for stack in (installed, list(DEFAULT_MIDDLEWARE)):
        assert [name for name in order if name in stack] == stack


def section_of(text: str, heading: str) -> str:
    """The text under ``heading``, up to the next heading of its level or above."""
    level = heading.split(" ", 1)[0]
    _, _, rest = text.partition(f"\n{heading}\n")
    return re.split(rf"\n#{{1,{len(level)}}} ", rest, maxsplit=1)[0]


ADRS = DOCS / "adr"
_STATUS = re.compile(r"^- Status: (.*)$", re.MULTILINE)
_STATUS_WORD = re.compile(r"(Proposed|Accepted|Superseded by \[\d{4}\]\([^)]+\))(;|$)")


def records() -> dict[str, Path]:
    """Every decision record, by number."""
    return {path.name[:4]: path for path in sorted(ADRS.glob("[0-9]" * 4 + "-*.md"))}


def status(record: Path) -> str:
    """The record's status line, as written."""
    match = _STATUS.search(record.read_text(encoding="utf-8"))
    assert match, f"{record.name} has no status line"
    return match[1]


def test_the_decision_index_lists_every_record_in_order_with_its_status() -> None:
    """One row per record, by number, each showing the record's own status."""
    index = (ADRS / "README.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| \[(\d{4})\]\(([^)]+)\) \| .*? \| (.*) \|$", index, re.M)

    assert [number for number, _, _ in rows] == list(records())
    for number, path, shown in rows:
        assert path == records()[number].name
        assert shown == status(records()[number]), f"{number}: copy its status line"


def test_every_status_names_its_amendments() -> None:
    """A status starts with the template's word and names what changed the record.

    Records that another ``- Amends:`` or ``- Supersedes:``, dated
    ``## Amendment``/``## Addendum`` sections at the end, and the earlier
    ``(Amended DATE.)`` marks each appear in the status line.
    """
    changed_by: dict[str, set[str]] = {}
    for number, record in records().items():
        header = record.read_text(encoding="utf-8").split("\n## ", 1)[0]
        for line in re.findall(r"^- (?:Amends|Supersedes): (.*)$", header, re.M):
            for target in re.findall(r"\[(\d{4})\]", line):
                changed_by.setdefault(target, set()).add(number)

    for number, record in records().items():
        text = record.read_text(encoding="utf-8")
        line = status(record)
        assert _STATUS_WORD.match(line), f"{number}: {line!r}"
        dates = re.findall(
            r"^## (?:Amendment|Addendum) \((\d{4}-\d\d-\d\d)\)", text, re.M
        ) + re.findall(r"\(Amended (\d{4}-\d\d-\d\d)\.\)", text)
        assert [date for date in dates if date not in line] == [], number
        assert [
            other for other in changed_by.get(number, ()) if f"[{other}](" not in line
        ] == [], number


SETTINGS_TABLES: tuple[tuple[type[BaseModel], str, str], ...] = (
    (Settings, "reference.md", "Setting"),
    (DatabaseConfig, "reference.md", "DATABASES"),
    (LedgerConfig, "reference.md", "LEDGERS"),
    (ShardConfig, "reference.md", "SHARDS"),
    (CacheConfig, "reference.md", "CACHES"),
    (StorageConfig, "reference.md", "STORAGES"),
    (BrokerConfig, "reference.md", "BROKERS"),
    (HttpClientConfig, "reference.md", "HTTP_CLIENT"),
    (DebugToolbarConfig, "reference.md", "DEBUG_TOOLBAR"),
    (MessagingSettings, "contrib.md", "MESSAGING"),
    (WebhookSettings, "contrib.md", "WEBHOOKS"),
    (AuditSettings, "contrib.md", "AUDIT"),
    (IdempotencySettings, "contrib.md", "IDEMPOTENCY"),
    (EvidenceSettings, "contrib.md", "EVIDENCE"),
    (FlagsSettings, "contrib.md", "FLAGS"),
    (AuthSettings, "auth.md", "AUTH"),
    (CookieSettings, "auth.md", 'AUTH["COOKIE"]'),
    (MfaSettings, "auth.md", 'AUTH["MFA"]'),
    (ClientCertSettings, "auth.md", 'AUTH["CLIENT_CERT"]'),
    (PrincipalCacheSettings, "auth.md", 'AUTH["PRINCIPAL_CACHE"]'),
    (JWTSettings, "auth.md", 'AUTH["JWT"]'),
    (MailSettings, "mail.md", "MAIL"),
    (SmtpSettings, "mail.md", "MAIL.SMTP"),
    (NotificationsSettings, "mail.md", "NOTIFICATIONS"),
)
"""Each settings model, the page of its table, and how the table is found."""
PROSE_DEFAULTS = {
    "Settings": {"SETTINGS_MODULE", "BASE_DIR", "MIDDLEWARE", "LOGGING"}
    | {"HTTP_CLIENT", "DEBUG_TOOLBAR"},
    "DatabaseConfig": {"URL", "COMMAND_TIMEOUT", "POOL_SIZE", "MAX_OVERFLOW"}
    | {"POOL_TIMEOUT", "POOL_RECYCLE"},
    "LedgerConfig": {"URL"},
    "ShardConfig": {"DATABASES", "RANGES"},
    "CacheConfig": {"KEY_PREFIX", "TLS_CA"},
    "BrokerConfig": {"URL", "PREFIX", "TLS_CA"},
    "AuthSettings": {"USER_TABLE", "PASSWORD_HASHERS", "PASSWORD_VALIDATORS"}
    | {"JWT", "COOKIE", "MFA", "CLIENT_CERT", "PRINCIPAL_CACHE"},
    "CookieSettings": {"NAME", "CSRF_NAME"},
    "JWTSettings": {"SIGNING_KEY"},
    "MailSettings": {"SMTP"},
}
"""Defaults the tables state in words (computed, required, or "see below")."""
_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}


def settings_tables(page: str) -> dict[str, dict[str, tuple[str, str]]]:
    """Each table of ``page`` with a Default column: {key: {name: (default, row)}}.

    A table is known by its first header cell (``AUTH`` key), by the
    ``Each `X` entry`` line above a ``Key`` table, or as ``Setting``: the
    top-level settings, whose tables the reference page splits by subject.
    """
    lines = (DOCS / page).read_text(encoding="utf-8").splitlines()
    tables: dict[str, dict[str, tuple[str, str]]] = {}
    for number, line in enumerate(lines):
        if not line.startswith("| --- |") or "| Default |" not in lines[number - 1]:
            continue
        first = lines[number - 1].strip("| ").split(" | ")[0]
        above = "\n".join(lines[max(0, number - 8) : number - 1])
        if named := re.fullmatch(r"`([^`]+)`(?: key)?", first):
            key = named[1]
        elif first == "Key":
            key = re.findall(r"Each `([A-Z_]+)` (?:entry|group)", above)[-1]
        else:
            key = first
        rows = tables.setdefault(key, {})
        for row in lines[number + 1 :]:
            if not row.startswith("|"):
                break
            names, default, *meaning = row.strip("| ").split(" | ")
            for name in re.findall(r"`([^`]+)`", names):
                rows[name] = (default, " | ".join([default, *meaning]))
    return tables


def documented_default(cell: str) -> tuple[bool, Any]:
    """The value a Default cell states, if it states one (not in words)."""
    if code := re.fullmatch(r"`([^`]*)`", cell):
        try:
            return True, ast.literal_eval(code[1])
        except (ValueError, SyntaxError):
            return True, code[1]  # memory://, dotted paths, ...
    if spans := re.fullmatch(r"`[^`]+`(?:, `[^`]+`)*", cell):
        return True, tuple(re.findall(r"`([^`]+)`", spans[0]))
    if amount := re.fullmatch(r"(\d+(?:\.\d+)?) (second|minute|hour|day)s?", cell):
        return True, float(amount[1]) * _UNITS[amount[2]]
    if re.fullmatch(r"\d+(?:\.\d+)?", cell):
        return True, ast.literal_eval(cell)
    return False, cell


def bounds(info: FieldInfo) -> list[tuple[str, float]]:
    """The limits a field enforces that a reader needs told (not "positive")."""
    found = []
    for constraint in info.metadata:
        for name in ("gt", "ge", "lt", "le", "min_length", "max_length"):
            value = getattr(constraint, name, None)
            if isinstance(value, timedelta):
                value = value.total_seconds()
            if value is not None and not (name in {"gt", "ge"} and value in (0, 1)):
                found.append((name, float(value)))
    return found


def spellings(value: float) -> set[str]:
    """How a row may write ``value``: ``86400``, ``86,400``, ``1 day``, ``10 MiB``."""
    forms = {str(value), f"{value:,}"}
    if value.is_integer():
        whole = int(value)
        forms |= {str(whole), f"{whole:,}"}
        for unit, seconds in _UNITS.items():
            if whole >= seconds and whole % seconds == 0:
                count = whole // seconds
                forms |= {f"{count:,} {unit}", f"{count:,} {unit}s"}
                if count == 1:
                    forms.add(f"{'an' if unit == 'hour' else 'a'} {unit}")
        for unit, size in (("KiB", 2**10), ("MiB", 2**20), ("GiB", 2**30)):
            if whole >= size and whole % size == 0:
                forms.add(f"{whole // size:,} {unit}")
    return forms


def states(text: str, value: float) -> bool:
    """Whether ``text`` writes ``value`` in one of its spellings, as a whole number."""
    return any(
        re.search(rf"(?<!\d)(?<!\d[,.]){re.escape(form)}(?!\d|[,.]\d)", text)
        for form in spellings(value)
    )


@pytest.mark.parametrize(
    ("model", "page", "key"),
    SETTINGS_TABLES,
    ids=[model.__name__ for model, _, _ in SETTINGS_TABLES],
)
def test_every_setting_row_states_its_default_and_bounds(
    model: type[BaseModel], page: str, key: str
) -> None:
    """Each field has a row whose default is the model's, and whose text its limits.

    A default in code (`"local"`, `10.0`, `()`) must validate to the model's
    own; one in words must be listed in ``PROSE_DEFAULTS``. Every bound the
    model enforces (``le=86400``, ``max_length=200``) is written in the row.
    """
    rows = settings_tables(page)[key]
    prose = PROSE_DEFAULTS.get(model.__name__, set())
    problems = []
    for name, info in model.model_fields.items():
        if name not in rows:
            problems.append(f"{name}: no row")
            continue
        cell, text = rows[name]
        parsed, value = documented_default(cell)
        if name in prose:
            if parsed:
                problems.append(f"{name}: states {cell}; take it out of PROSE_DEFAULTS")
        elif not parsed:
            problems.append(f"{name}: default {cell!r} is in words")
        else:
            real = model.model_fields[name].get_default(call_default_factory=True)
            if isinstance(value, str) and value.startswith("..."):
                value = (real or "") if str(real).endswith(value[3:]) else value
            if isinstance(value, str) and isinstance(real, tuple):
                value = tuple(value.split())  # `GET POST PUT PATCH DELETE`
            if value == "<BASE_DIR>/migrations":
                value = project_root() / "migrations"
            annotation: Any = info.annotation  # Every settings field has one.
            stated = TypeAdapter(annotation).validate_python(value)
            if stated != real:
                problems.append(f"{name}: documented {cell}, but it is {real!r}")
        for kind, limit in bounds(info):
            if kind == "min_length" and limit == 1:
                if not re.search(r"\bempty\b|at least one|one or more", text):
                    problems.append(f"{name}: say that it may not be empty")
            elif not states(text, limit):
                problems.append(f"{name}: the limit {limit:,.12g} is not stated")
    assert problems == []


AUDIT_PAGE = DOCS / "audit" / "0.9.0.md"
_AUDIT_ID = re.compile(
    r"\b(?:DJ-\d+|N-?\d+|ASYNC-R?\d+|R\d{2}|FX-\d+|SEC-r\d-\d+|"
    r"(?:PERF|FIN|AUTH|CONC|WEB|DATA|TEST|INFRA|DOCS|DSGN)-\d+)\b"
)


def test_every_audit_id_the_changelog_cites_is_on_the_audit_page() -> None:
    """A reader can look up each ``(FIN-77)``; ``R22`` is listed as ``ASYNC-R22``."""
    page = AUDIT_PAGE.read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    cited = {
        f"ASYNC-{ident}" if re.fullmatch(r"R\d{2}", ident) else ident
        for ident in _AUDIT_ID.findall(changelog)
    }

    assert sorted(i for i in cited if not re.search(rf"\b{i}\b", page)) == []
    assert "docs/audit/0.9.0.md" in changelog.split("## [Unreleased]")[0]


def test_open_security_findings_are_listed_without_their_detail() -> None:
    """Until a release fixes one, the audit page names its id and severity only."""
    page = AUDIT_PAGE.read_text(encoding="utf-8")
    security = re.findall(r"^### Security: .*?(?=^#)", page, re.MULTILINE | re.DOTALL)

    rows = [
        row.split(" | ")
        for table in security
        for row in table.splitlines()
        if row.startswith("| ") and row.endswith(" | Open |")
    ]
    assert rows
    shown = [row[0] for row in rows if not row[1].startswith("Withheld while open")]
    assert shown == []
