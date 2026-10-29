"""Expected versions: change an aggregate only if it is still as the caller saw it.

A client that read version 3 of an order, and sends a change with
``If-Match``, must not overwrite version 4, which someone else stored
meanwhile. The resource layer turns ``If-Match`` into an expectation the
unit of work holds around the use case; its repositories check it when they
first hand the aggregate out, inside the transaction that then changes it::

    with uow.expect(Order, order_id, {3}) as expected:
        await services.cancel_order(command, uow)    # loads the order: checked
    assert expected.checked

A stale aggregate raises ``PreconditionFailed`` (412), and the unit of work
rolls back; a commit before the aggregate was loaded at all is refused, as
its version could not be checked. Optimistic locking (the ``version``
column) still guards the commit against a change that lands after the
check. An expectation is checked when the aggregate is first loaded in
each transaction of the unit of work, and not again in that transaction,
since the use case's own changes move the version on afterwards. A
version the unit of work's own commit produced is accepted from then on;
any other, stored by another client between two transactions, is refused.

The expectation belongs to that unit of work alone: another one the use
case opens (an audit record, a lookup) neither checks nor needs it. Until
0.12 it could be held for the whole context instead (``expect_version``),
every unit of work in it refusing to commit before the aggregate was
loaded; 0.13 removed that.
"""

from collections.abc import Collection, Hashable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from domainkit.ddd.exceptions import PreconditionFailed

__all__ = [
    "Expectation",
]


@dataclass(slots=True)
class Expectation:
    """The version an aggregate must have when a use case loads it.

    ``UnitOfWork.expect`` makes one; ``checked`` says, after the block,
    whether the use case loaded the aggregate.
    """

    model: type
    """The aggregate's class (subclasses count)."""
    id: Hashable
    versions: frozenset[int] | None
    """The versions accepted; None accepts any (``If-Match: *``)."""
    checked: bool = False
    """Whether a repository has handed the aggregate out yet."""
    held: bool = False
    """Whether it was checked in the unit of work's current transaction."""

    def matches(self, aggregate: object) -> bool:
        """Whether ``aggregate`` is the one expected."""
        return (
            isinstance(aggregate, self.model)
            and getattr(aggregate, "id", None) == self.id
        )

    def check(self, aggregate: object) -> None:
        """Check ``aggregate`` if it is the one expected and not checked yet.

        It is checked once per transaction: the first load in each one the
        unit of work begins (``renew``) is checked again, so a change
        another client stored between two transactions is refused.

        Raises:
            PreconditionFailed: Its version is not one of ``versions``.
        """
        if self.held or not self.matches(aggregate):
            return
        self.held = self.checked = True
        version = getattr(aggregate, "version", None)
        if self.versions is not None and version not in self.versions:
            raise PreconditionFailed(
                "This was changed since you read it; read it again before changing it."
            )

    def renew(self) -> None:
        """A new transaction begins: its first load is checked again."""
        self.held = False

    def accept(self, aggregate: object) -> None:
        """Accept the version ``aggregate`` has now, if it is the one expected.

        The unit of work calls it once it committed: the version its own
        change produced is no other client's, so a later transaction may
        load it.
        """
        if self.versions is not None and self.matches(aggregate):
            self.versions = self.versions | {cast(Any, aggregate).version}


def expectation(
    model: type, id_: Hashable, versions: Collection[int] | None
) -> Expectation:
    """Expect aggregate ``id_`` of ``model`` at one of ``versions``."""
    return Expectation(model, id_, None if versions is None else frozenset(versions))


def check_loaded(own: Sequence[Expectation]) -> None:
    """Refuse to commit before every aggregate expected was loaded and checked.

    Units of work call it before they commit, with their own expectations
    (``own``), so a use case that changes an aggregate without reading it
    through its repository writes nothing.

    Raises:
        RuntimeError: An expected aggregate was not loaded yet.
    """
    for expectation in own:
        if not expectation.checked:
            raise RuntimeError(
                f"The service did not load {expectation.model.__name__} "
                f"{expectation.id} through its repository before committing, so "
                "If-Match could not be checked; nothing was written."
            )


def check_expected(aggregate: object, own: Sequence[Expectation]) -> None:
    """Hold ``aggregate`` to the repository's expectations.

    Repositories call it for each aggregate they load, with those of the
    unit of work that lent them out (``own``).

    Raises:
        PreconditionFailed: The aggregate is stale.
    """
    for expectation in own:
        expectation.check(aggregate)
