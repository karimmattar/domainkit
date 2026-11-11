"""SEC-16a-4: ``POST /auth/tokens`` checks 14,400 passwords a day for a session.

AUTH-16's fix asks users without a second factor for their ``password``
before ``POST /auth/tokens`` makes a token, so that a page script riding
a cookie session (the AUTH-16 threat) cannot mint a lasting bearer token.
The check (``stepup.check_password_again``) counts wrong passwords at
``LOGIN_RATE`` (10 a minute) per user and, unless
``PASSWORD_CHECKS_COUNT_AS_LOGINS``, not against ``ACCOUNT_LOGIN_RATE``,
which AUTH-19's fix added to bound guessing to NIST's 100. So that script
(or anyone holding a session) gets an oracle for the password at 14,400
guesses a day; the password it finds mints the token, survives logout,
and passes every later password check. The other signed-in checks
(``/auth/password``, a step-up) share the count.

Here a session sends 101 wrong passwords at ``LOGIN_RATE``'s pace, within
20 minutes: every one is checked (400), none refused (429).
"""

from datetime import timedelta

from domainkit.clock import freeze_time
from domainkit_tests.conftest import Api
from domainkit_tests.support.auth import create_user
from domainkit_tests.test_auth_personal_tokens import make, user_session


def test_a_session_has_at_most_100_password_guesses_checked_a_day(
    identity: Api,
) -> None:
    """Signed-in password checks are bounded as logins are: 100 a day."""
    create_user("alice")
    client = identity(LOGIN_RATE="10/minute", ACCOUNT_LOGIN_RATE="100/day")
    with freeze_time("2026-11-01T00:00:00Z") as clock:
        headers = user_session(client)
        answers = []
        for number in range(101):
            answers.append(
                make(client, headers, password=f"guess-{number}").status_code
            )
            clock.tick(timedelta(seconds=6))

    checked = answers.count(400)
    assert checked <= 100, f"{checked} passwords checked for one session in 11 minutes"
