"""What the stores share."""

from typing import Any


def changed_rows(result: Any) -> int:  # noqa: ANN401 - a CursorResult.
    """Rows a statement changed."""
    return int(result.rowcount)
