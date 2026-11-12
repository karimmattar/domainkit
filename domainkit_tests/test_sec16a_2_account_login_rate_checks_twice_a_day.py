"""SEC-16a-2: ``ACCOUNT_LOGIN_RATE = "100/day"`` checks about 200 guesses in a day.

AUTH-19's fix adds a per-account count, ``AUTH.ACCOUNT_LOGIN_RATE``,
default ``"100/day"``, documented as "the day's total, as NIST SP 800-63B
5.2.2 asks (100 failed attempts at most)", and in
``docs/auth/logging-in.md`` as "At most ``AUTH.ACCOUNT_LOGIN_RATE`` (100 a
day) wrong passwords are checked for one account over the day, however
slowly they come", "of which a stranger without a proof gets 100".

The count is a GCRA bucket (``http.ratelimit``): a burst of ``limit``,
refilled at one per ``period / limit`` (864 s). Its own module says so: "a
window of one period can see up to about twice ``limit``". A stranger who
spends the burst, then guesses once every 14.4 minutes, has 199 wrong
passwords checked against one account within 24 hours, within the default
``LOGIN_RATE`` too, from rotating addresses.
"""

from datetime import timedelta

from domainkit.clock import freeze_time
from domainkit_tests.conftest import Api
from domainkit_tests.support.auth import create_user
from domainkit_tests.test_auth_devices import guess

DAY = timedelta(days=1)


def test_a_stranger_has_at_most_100_guesses_checked_in_a_day(api: Api) -> None:
    """At the defaults, no 24 hours check more than 100 wrong passwords."""
    create_user("alice")
    client = api(LOGIN_RATE="10/minute", ACCOUNT_LOGIN_RATE="100/day")
    checked = 0
    with freeze_time("2026-11-01T00:00:00Z") as clock:
        elapsed = timedelta()
        number = 0

        def one() -> int:
            nonlocal number
            number += 1
            address = f"198.51.{number // 250}.{number % 250 + 1}"
            return guess(client.app, address)

        for _ in range(100):  # The burst, at LOGIN_RATE's pace.
            checked += one() == 401
            clock.tick(timedelta(seconds=6))
            elapsed += timedelta(seconds=6)
        step = timedelta(seconds=864)  # Then as the day's count frees places.
        while elapsed + step < DAY:
            clock.tick(step)
            elapsed += step
            checked += one() == 401

    assert checked <= 100, f"{checked} wrong passwords checked for alice within 24 h"
