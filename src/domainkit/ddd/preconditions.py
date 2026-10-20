"""Expected versions: change an aggregate only if it is still as the caller saw it.

A client that read version 3 of an order, and sends a change with
``If-Match``, must not overwrite version 4, which someone else stored
meanwhile. The resource layer turns ``If-Match`` into an expectation around
the use case; the repository checks it when it first hands the aggregate
out, inside the transaction that then changes it::

    with expect_version(Order, order_id, {3}) as expected:
        await services.cancel_order(command, uow)    # loads the order: checked
    assert expected.checked

A stale aggregate raises ``PreconditionFailed`` (412), and the unit of work
rolls back; a commit before the aggregate was loaded at all is refused, as
its version could not be checked. Optimistic locking (the ``version``
column) still guards the commit against a change that lands after the
check. Each expectation is checked once, when the aggregate is first
loaded, since the use case's own changes move the version on afterwards.
"""

from collections.abc import Collection, Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from domainkit.ddd.exceptions import PreconditionFailed


@dataclass(slots=True)
class Expectation:
    """The version an aggregate must have when a use case loads it."""

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


@contextmanager
def expect_version(
    model: type, id_: Hashable, versions: Collection[int] | None
) -> Iterator[Expectation]:
    """Expect aggregate ``id_`` of ``model`` at one of ``versions`` in the block.

    ``versions`` None only notes whether the aggregate is loaded.
    """
    expectation = Expectation(
        model, id_, None if versions is None else frozenset(versions)
    )
    token = _expected.set((*_expected.get(), expectation))
    try:
        yield expectation
    finally:
        _expected.reset(token)


def check_loaded() -> None:
    """Refuse to commit before every aggregate expected was loaded and checked.

    Units of work call it before they commit, so a use case that changes
    an aggregate without reading it through its repository writes nothing.

    Raises:
        RuntimeError: An expected aggregate was not loaded yet.
    """
    for expectation in _expected.get():
        if not expectation.checked:
            raise RuntimeError(
                f"The service did not load {expectation.model.__name__} "
                f"{expectation.id} through its repository before committing, so "
                "If-Match could not be checked; nothing was written."
            )


def check_expected(aggregate: object) -> None:
    """Hold ``aggregate`` to every expectation of the current context.

    Repositories call it for each aggregate they load.

    Raises:
        PreconditionFailed: The aggregate is stale.
    """
    for expectation in _expected.get():
        expectation.check(aggregate)
