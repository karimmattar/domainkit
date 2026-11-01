"""INFRA-14: a job that gates nothing on a pull request gates the release.

``release.yml`` publishes a tag only when the CI run of its commit's push
to ``main`` concluded ``success``: the whole run, every job in it. On a
push, ``stress (3.14t)`` runs too, though ``test_ci.ADVISORY`` and
``docs/versioning.md`` say it gates nothing, because on 3.14.6t some of its
30 runs crash inside SQLAlchemy's compiled extensions. One crash makes the
push run ``failure``, and ``release.yml`` then refuses the tag of a commit
that passed every required check. Either an advisory job does not run on
a push to ``main`` (or cannot fail it, ``continue-on-error``), or the
release reads the conclusions of the required jobs alone.
"""

from domainkit_tests.test_ci import ADVISORY, jobs, running


def test_no_advisory_job_can_fail_the_run_a_release_requires_green() -> None:
    """Only the checks the ruleset requires decide whether main's run passed."""
    script = jobs("release.yml")["ci"]["steps"][0]["run"]
    if "--json jobs" in script:  # The release reads each required job instead.
        return
    gating = {
        name
        for name in running("push") & ADVISORY
        if jobs()[name].get("continue-on-error") is not True
    }
    assert gating == set()
