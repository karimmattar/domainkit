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
check. Each expectation is checked once, when the aggregate is first
loaded, since the use case's own changes move the version on afterwards.

The expectation belongs to that unit of work alone: another one the use
case opens (an audit record, a lookup) neither checks nor needs it. Until
0.12 it was held for the whole context instead (``expect_version``), and
every unit of work in it refused to commit before the aggregate was loaded;
that still works, deprecated, until 0.13.
"""

import warnings
from collections.abc import Collection, Hashable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from domainkit.ddd.exceptions import PreconditionFailed
from domainkit.deprecation import DomainkitDeprecationWarning

__all__ = [
    "Expectation",
    "expect_version",
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

    def check(self, aggregate: object) -> None:
        """Check ``aggregate`` if it is the one expected and not checked yet.

        Raises:
            PreconditionFailed: Its version is not one of ``versions``.
        """
        if (
            self.checked
            or not isinstance(aggregate, self.model)
            or getattr(aggregate, "id", None) != self.id
        ):
            return
        self.checked = True
        version = getattr(aggregate, "version", None)
        if self.versions is not None and version not in self.versions:
            raise PreconditionFailed(
                "This was changed since you read it; read it again before changing it."
            )


_expected: ContextVar[tuple[Expectation, ...]] = ContextVar(
    "domainkit_expected_versions", default=()
)


def expectation(
    model: type, id_: Hashable, versions: Collection[int] | None
) -> Expectation:
    """Expect aggregate ``id_`` of ``model`` at one of ``versions``."""
    return Expectation(model, id_, None if versions is None else frozenset(versions))


@warnings.deprecated(
    "Use uow.expect(model, id_, versions), held by the unit of work that "
    "changes the aggregate; removed in domainkit 0.13.",
    category=DomainkitDeprecationWarning,
)
@contextmanager
def expect_version(
    model: type, id_: Hashable, versions: Collection[int] | None
) -> Iterator[Expectation]:
    """Expect aggregate ``id_`` of ``model`` at one of ``versions`` in the block.

    ``versions`` None only notes whether the aggregate is loaded. Every
    repository and unit of work of the context holds to it.

    Deprecated: use ``UnitOfWork.expect``, which only that unit of work
    holds to; removed in 0.13.
    """
    made = expectation(model, id_, versions)
    token = _expected.set((*_expected.get(), made))
    try:
        yield made
    finally:
        _expected.reset(token)


def check_loaded(own: Sequence[Expectation] = ()) -> None:
    """Refuse to commit before every aggregate expected was loaded and checked.

    Units of work call it before they commit, with their own expectations
    (``own``), so a use case that changes an aggregate without reading it
    through its repository writes nothing. Those of the context
    (``expect_version``, deprecated) are held too.

    Raises:
        RuntimeError: An expected aggregate was not loaded yet.
    """
    for expectation in (*own, *_expected.get()):
        if not expectation.checked:
            raise RuntimeError(
                f"The service did not load {expectation.model.__name__} "
                f"{expectation.id} through its repository before committing, so "
                "If-Match could not be checked; nothing was written."
            )


def check_expected(aggregate: object, own: Sequence[Expectation] = ()) -> None:
    """Hold ``aggregate`` to the repository's expectations, and the context's.

    Repositories call it for each aggregate they load, with those of the
    unit of work that lent them out (``own``).

    Raises:
        PreconditionFailed: The aggregate is stale.
    """
    for expectation in own:
        expectation.check(aggregate)
    for expectation in _expected.get():
        expectation.check(aggregate)
