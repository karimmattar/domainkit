"""The old name of ``domainkit.contrib.evidence.models``; removed in 0.11.

Kept so imports from 0.9.1 and earlier still work, each with a
``DomainkitDeprecationWarning``; import from the package
(``domainkit.contrib.evidence``) instead.
"""

import warnings
from typing import Final

from domainkit.contrib.evidence import models
from domainkit.deprecation import DomainkitDeprecationWarning

NAMES: Final = frozenset(
    {
        "DIGEST",
        "FORMAT",
        "KIND",
        "SUBJECT",
        "Evidence",
        "EvidenceConflict",
        "EvidenceNotFound",
        "EvidenceReceipt",
        "EvidenceTampered",
        "RecordedEvidence",
        "sha256",
    }
)
"""The public names this module had in 0.9.1."""


def __getattr__(name: str) -> object:
    """``name`` from ``models``, with a warning naming where to import it from.

    Raises:
        AttributeError: ``name`` was never public here.
    """
    if name not in NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"domainkit.contrib.evidence.model is deprecated; import {name} from "
        "domainkit.contrib.evidence (or its models). Removed in 0.11.",
        DomainkitDeprecationWarning,
        stacklevel=2,
    )
    return getattr(models, name)
