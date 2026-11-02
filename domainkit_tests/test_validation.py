"""One validation path: objects check their own fields' markers, and ``clean()``."""

from decimal import Decimal
from typing import TYPE_CHECKING, Annotated
from uuid import UUID, uuid4

import pytest

from domainkit.ddd import (
    AggregateRoot,
    BusinessRuleViolation,
    Command,
    DomainEvent,
    InvalidField,
    MaxLength,
    MinLength,
    Pattern,
    Range,
    Repository,
    UnitOfWork,
    ValueObject,
    in_memory,
)
from domainkit.ddd._validation import checks_of

if TYPE_CHECKING:
    from domainkit_tests.gadgets import Gadget

pytestmark = pytest.mark.anyio

Code = Annotated[str, MinLength(2), MaxLength(4), Pattern("[A-Z]+")]


class Account(AggregateRoot[UUID]):
    """Fields with markers, one optional, one in a union, one without."""

    code: Code
    balance: Annotated[Decimal, Range(min=0)] = Decimal(0)
    nickname: Annotated[str, MaxLength(3)] | None = None
    either: Annotated[int, Range(max=1)] | str = 0
    note: str = ""

    def clean(self) -> None:
        """A rule across fields: a code of ``ZERO`` holds nothing."""
        if self.code == "ZERO" and self.balance:
            raise BusinessRuleViolation("ZERO accounts hold nothing.")


class Accounts(UnitOfWork):
    """The port of a unit of work over accounts."""

    accounts: Repository[Account, UUID]


class Code3(ValueObject):
    """A value object with a marker."""

    value: Annotated[str, MaxLength(3)]


class Label(ValueObject):
    """A value object whose marked field may be empty."""

    text: Annotated[str, MaxLength(3)] | None = None


class Traced(AggregateRoot[UUID]):
    """An entity that writes its own ``__setattr__``; the class keeps it."""

    code: Annotated[str, MaxLength(2)] = "a"

    def __setattr__(self, name: str, value: object) -> None:
        """Upper-case codes, then set as the base does."""
        if name == "code" and isinstance(value, str):
            value = value.upper()
        super().__setattr__(name, value)


class Deposit(Command):
    """A command with a marker."""

    amount: Annotated[int, Range(min=1)]


class Deposited(DomainEvent):
    """An event with a marker."""

    amount: Annotated[int, Range(min=1)]


class Later(AggregateRoot[UUID]):
    """A hint that names a class only type checkers import."""

    code: Annotated[str, MaxLength(2)] = "a"
    gadget: "Gadget | None" = None


def account(**fields: object) -> Account:
    """A valid account, with ``fields`` replaced."""
    return Account(**{"id": uuid4(), "code": "AB", **fields})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("fields", "field", "message"),
    [
        ({"code": "A"}, "code", "code: Use at least 2 characters."),
        ({"code": "ABCDE"}, "code", "code: Use at most 4 characters."),
        ({"code": "ab"}, "code", "code: Enter a value in the expected format."),
        ({"balance": Decimal(-1)}, "balance", "balance: Use 0 or more."),
        ({"nickname": "long"}, "nickname", "nickname: Use at most 3 characters."),
    ],
)
def test_an_entity_refuses_what_its_markers_rule_out_when_made(
    fields: dict[str, object], field: str, message: str
) -> None:
    """Each marker, on its own field, with the field named."""
    with pytest.raises(InvalidField) as caught:
        account(**fields)

    assert (caught.value.field, caught.value.message) == (field, message)
    assert caught.value.code == "invalid_field"


def test_an_entity_refuses_a_change_its_markers_rule_out() -> None:
    """Every later set is checked; the field keeps its value."""
    held = account()

    with pytest.raises(InvalidField, match="balance: Use 0 or more"):
        held.balance = Decimal("-0.01")
    held.balance = Decimal(5)
    held.nickname = None
    held.note = "x" * 1000

    assert (held.balance, held.nickname) == (Decimal(5), None)


def test_markers_check_only_values_of_their_kind() -> None:
    """None passes; a union of two types checks nothing; other kinds pass."""
    held = account(either="anything", nickname=None)

    held.either = 9  # The union has two parts, so no marker applies.

    assert checks_of(Account).keys() == {"code", "balance", "nickname"}
    assert Range(min=0).problem("text") is None
    assert MaxLength(1).problem(5) is None
    assert MinLength(2).problem(None) is None
    assert Pattern("[0-9]+").problem(3) is None
    assert Range(max=1).problem(True) is None
    assert Range(min=1, max=3).problem(2) is None
    assert Range(max=3).problem(Decimal("3.5")) == "Use 3 or less."


def test_a_load_is_not_checked() -> None:
    """Data set as an ORM sets it, past ``__setattr__``, is kept as stored."""
    held = account()

    held.__dict__["code"] = "toolong"

    assert held.code == "toolong"


def test_value_objects_commands_and_events_check_their_fields_once() -> None:
    """Frozen classes check after ``__init__``."""
    with pytest.raises(InvalidField, match="value: Use at most 3"):
        Code3("abcd")
    with pytest.raises(InvalidField, match="amount: Use 1 or more"):
        Deposit(amount=0)
    with pytest.raises(InvalidField, match="amount: Use 1 or more"):
        Deposited(amount=0)

    assert (Code3("abc").value, Deposit(amount=1).amount) == ("abc", 1)
    assert Label().text is None
    with pytest.raises(InvalidField, match="text: Use at most 3"):
        Label("abcd")


def test_an_entity_s_own_setattr_is_kept_and_still_checks() -> None:
    """The value given is checked before the class's own ``__setattr__`` sees it."""
    traced = Traced(id=uuid4(), code="ab")

    with pytest.raises(InvalidField, match="code: Use at most 2"):
        traced.code = "abc"

    assert traced.code == "AB"


def test_hints_not_resolved_yet_keep_the_markers_of_the_others() -> None:
    """A name only type checkers know does not lose the other fields' checks."""
    with pytest.raises(InvalidField, match="code: Use at most 2"):
        Later(id=uuid4(), code="abc")

    assert checks_of(Later).keys() == {"code"}


def test_clean_runs_when_an_entity_is_made() -> None:
    """Rules across fields hold from the start."""
    with pytest.raises(BusinessRuleViolation, match="ZERO accounts hold nothing"):
        account(code="ZERO", balance=Decimal(1))


async def _fund(uow: Accounts, account_id: UUID) -> None:
    """Give a ZERO account a balance, alone: fine for the field, not for clean()."""
    async with uow:
        changed = await uow.accounts.get(account_id)
        changed.balance = Decimal(1)
        await uow.commit()


async def test_clean_runs_before_every_commit_and_nothing_is_stored() -> None:
    """A change field by field may pass through a broken rule; a commit may not."""
    uow = in_memory(Accounts)
    held = account(code="ZERO")
    async with uow:
        await uow.accounts.add(held)
        await uow.commit()

    with pytest.raises(BusinessRuleViolation):
        await _fund(uow, held.id)
    async with uow:
        changed = await uow.accounts.get(held.id)
        changed.balance = Decimal(2)
        changed.code = "MAIN"  # Both changed: the rule holds at commit.
        await uow.commit()

    assert uow.committed == 2
    assert (await uow.accounts.get(held.id)).code == "MAIN"
