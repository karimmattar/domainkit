"""The old name of ``domainkit.contrib.evidence.models``, which every app now uses.

Kept so imports from 0.9.1 and earlier still work; import from the package
(``domainkit.contrib.evidence``) instead.
"""

from domainkit.contrib.evidence.models import DIGEST as DIGEST
from domainkit.contrib.evidence.models import FORMAT as FORMAT
from domainkit.contrib.evidence.models import KIND as KIND
from domainkit.contrib.evidence.models import SUBJECT as SUBJECT
from domainkit.contrib.evidence.models import Evidence as Evidence
from domainkit.contrib.evidence.models import EvidenceConflict as EvidenceConflict
from domainkit.contrib.evidence.models import EvidenceNotFound as EvidenceNotFound
from domainkit.contrib.evidence.models import EvidenceReceipt as EvidenceReceipt
from domainkit.contrib.evidence.models import EvidenceTampered as EvidenceTampered
from domainkit.contrib.evidence.models import RecordedEvidence as RecordedEvidence
from domainkit.contrib.evidence.models import sha256 as sha256
