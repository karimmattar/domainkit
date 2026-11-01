"""INFRA-14: a job that gates nothing on a pull request gated the release.

``release.yml`` published a tag only when the CI run of its commit's push
to ``main`` concluded ``success``: the whole run, every job in it. On a
push, ``stress (3.14t)`` runs too, though ``test_ci.ADVISORY`` and
``docs/versioning.md`` say it gates nothing, because on 3.14.6t some of its
30 runs crash inside SQLAlchemy's compiled extensions. One crash made the
push run ``failure``, and ``release.yml`` then refused the tag of a commit
that passed every required check. The release now judges the run by the
checks the main branch ruleset requires, and ``attest``.
"""

from domainkit_tests.test_ci import ADVISORY, jobs, judge, push_run, running


def test_no_advisory_job_can_fail_the_run_a_release_requires_green() -> None:
    """Only the checks the ruleset requires decide whether main's run passed."""
    [*_, find] = jobs("release.yml")["ci"]["steps"]
    assert "--json conclusion" not in find["run"]
    advisory = running("push") & ADVISORY
    assert advisory == {"free-threaded-stress"}
    failed = {jobs()[name].get("name", name): "failure" for name in advisory}
    assert judge(push_run(**failed)).returncode == 0
    assert judge(push_run(**{"test (3.14t)": "failure"})).returncode == 1
