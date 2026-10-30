"""SEC-13c-1: the upgrade guide's test never reads a Breaking removal.

``test_the_upgrade_guide_covers_every_breaking_change_and_deprecation``
holds ``docs/upgrading.md`` to the audit ids of the newest release's
Breaking entries, but ``_upgrade_entries`` collects them from ``### Changed``
(and every ``### Deprecated`` one) only. 0.13's removals are filed under
``### Removed``: the hook methods (an ``erase_subject`` left behind keeps a
person's data, DSGN-03) and ``expect_version`` (``If-Match``, DSGN-14). A
Breaking entry under ``### Security`` is skipped the same way. Either step
could leave the guide with no test failing.
"""

from domainkit_tests.test_docs import ROOT, _upgrade_entries, upgrade_entries


def test_breaking_entries_of_removed_and_security_are_read() -> None:
    """A Breaking entry is one to act on, whichever section files it."""
    changelog = (
        "# Changelog\n\n## [Unreleased]\n\n### Changed\n\n"
        "- **Breaking:** changed (C-1).\n\n### Removed\n\n"
        "- **Breaking:** removed (R-1).\n\n### Security\n\n"
        "- **Breaking:** secured (S-1).\n"
    )

    assert upgrade_entries(changelog) == [
        "**Breaking:** changed (C-1).\n\n",
        "**Breaking:** removed (R-1).\n\n",
        "**Breaking:** secured (S-1).\n",
    ]


def test_this_releases_removals_are_held_to_the_guide() -> None:
    """DSGN-03 and DSGN-14, both Breaking removals of 0.13, are checked.

    Read from 0.13.0's section: once ``[Unreleased]`` has Breaking entries
    of its own, the guide is held to those instead.
    """
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    entries = _upgrade_entries(changelog.split("\n## [0.13.0]")[1].split("\n## [")[0])

    assert [
        ident
        for ident in ("DSGN-03", "DSGN-14")
        if not any(ident in entry for entry in entries)
    ] == []
