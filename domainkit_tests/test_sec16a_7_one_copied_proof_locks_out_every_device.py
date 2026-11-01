"""SEC-16a-7: one copied device proof locks every device of the owner out for a day.

ADR 0087's device proofs keep a user's known browsers in while strangers
flood the account, and ``docs/auth/logging-in.md`` bounds what a copied
proof can do: "One copied proof fills only its own device's count, half
the ceiling." AUTH-19's fix adds a daily count for proven attempts,
``account_key("proven-daily", username)`` at ``ACCOUNT_LOGIN_RATE``
(``api._login_limits``): one count per account, shared by every proof.

So whoever holds one proof for the account (an old laptop, a shared
computer, a cookie copied by malware) fills it with 100 wrong passwords
in about ten minutes at the device's own ``LOGIN_RATE``, and every
browser the owner logged in from is refused (429) for the right password
for as long as the copy sends one guess each 14.4 minutes (here, six
hours later still); strangers fill the unproven count meanwhile
and keep new browsers out too. Before the fix the copy filled only its
own device's count, and the owner's other browsers got in.
"""

from datetime import timedelta

from domainkit.clock import freeze_time
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_devices import RIGHT, browser
from domainkit_tests.test_auth_http import create_user


def test_a_copied_proof_leaves_the_owners_other_browsers_in(api: Api) -> None:
    """The copy's 100 wrong passwords fill its own count, not the laptop's."""
    create_user("alice")
    client = api(LOGIN_RATE="10/minute", ACCOUNT_LOGIN_RATE="100/day")
    # Each client runs its requests on one loop, so on one pool: a client
    # outside ``with`` starts a loop, and an engine, for every request.
    with (
        freeze_time("2026-11-01T08:00:00Z") as clock,
        browser(client.app, "203.0.113.50") as laptop,
        browser(client.app, "203.0.113.51") as old_pc,
        browser(client.app, "198.51.100.7") as copy,
    ):
        assert laptop.post("/auth/login", json=RIGHT).status_code == 200
        assert old_pc.post("/auth/login", json=RIGHT).status_code == 200
        copy.cookies = old_pc.cookies  # The old computer's proof, copied.

        wrong = {"username": "alice", "password": "guess"}
        guesses = []
        for _ in range(100):
            guesses.append(copy.post("/auth/login", json=wrong).status_code)
            clock.tick(timedelta(seconds=6))
        owner = laptop.post("/auth/login", json=RIGHT)
        for _ in range(25):  # Six hours on, one guess as each place frees.
            clock.tick(timedelta(seconds=864))
            copy.post("/auth/login", json=wrong)
        later = laptop.post("/auth/login", json=RIGHT)

    assert guesses.count(401) == 100
    assert (owner.status_code, later.status_code) == (200, 200), (
        "one copied proof locked the owner's laptop out"
    )
