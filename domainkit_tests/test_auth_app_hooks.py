"""What auth asks other apps, it asks through hooks (ADR 0140).

Refused attempts go to ``on_refusal`` hooks: audit registers its trail.
Making a superuser in the admin API passes ``on_make_superuser`` checks:
approvals registers its guard. Auth imports neither app.
"""

from fastapi import status

from domainkit.contrib.auth.gates import on_make_superuser
from domainkit.contrib.auth.integration import audit_refusal, on_refusal
from domainkit.ddd import PermissionDenied
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.support.auth import audited, create_user
from domainkit_tests.test_auth_admin import ADMIN, error, staff, usernames


def test_a_refusal_reaches_every_hook_without_what_was_tried(api: Api) -> None:
    """Each hook gets the action and the reason's code, in the order registered."""
    api()
    seen: list[tuple[str, str, str]] = []

    async def first(action: str, reason: str) -> None:
        seen.append(("first", action, reason))

    async def second(action: str, reason: str) -> None:
        seen.append(("second", action, reason))

    on_refusal(first, domain="auth")
    on_refusal(second, domain="auth")

    run_async(audit_refusal("auth.login", "wrong_password"))

    assert seen == [
        ("first", "auth.login", "wrong_password"),
        ("second", "auth.login", "wrong_password"),
    ]


def test_audit_registers_its_trail_for_auths_refusals(api: Api) -> None:
    """With audit installed, a refusal is a denied row; auth names no app for it."""
    api()

    assert [hook.domain for hook in on_refusal] == ["audit"]
    run_async(audit_refusal("auth.sample", "sample_reason"))

    [row] = audited("auth.sample")
    assert (row["outcome"], row["details"]) == ("denied", {"reason": "sample_reason"})


def test_a_check_refuses_making_a_superuser_in_the_admin_api(api: Api) -> None:
    """A check that raises stops both ways: creating one, and promoting a user."""
    client = api()
    root = staff("root", is_superuser=True)
    carol = create_user("carol")
    asked: list[str] = []

    def held() -> None:
        asked.append("asked")
        raise PermissionDenied("Superusers are made elsewhere.")

    on_make_superuser(held, domain="auth")

    created = client.post(
        f"{ADMIN}/users", json={"username": "boss", "is_superuser": True}, headers=root
    )
    promoted = client.patch(
        f"{ADMIN}/users/{carol}", json={"is_superuser": True}, headers=root
    )
    plain = client.post(f"{ADMIN}/users", json={"username": "dave"}, headers=root)

    refusal = ("permission_denied", "Superusers are made elsewhere.")
    assert error(created, status.HTTP_403_FORBIDDEN) == refusal
    assert error(promoted, status.HTTP_403_FORBIDDEN) == refusal
    assert plain.status_code == status.HTTP_201_CREATED
    assert asked == ["asked", "asked"]
    assert usernames(client) == {"root", "carol", "dave"}


def test_with_no_check_superusers_make_superusers(api: Api) -> None:
    """No app holds them back here: the admin API makes one, as before."""
    client = api()
    root = staff("root", is_superuser=True)

    assert list(on_make_superuser) == []
    boss = client.post(
        f"{ADMIN}/users", json={"username": "boss", "is_superuser": True}, headers=root
    )

    assert boss.json()["is_superuser"] is True
