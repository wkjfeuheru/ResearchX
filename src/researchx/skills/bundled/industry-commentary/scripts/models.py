"""Contract for industry-commentary."""

from typing import Literal
from researchx.skills._shared.reporting import ReportDocument


class ReportResult(ReportDocument):
    kind: Literal["industry-commentary"] = "industry-commentary"
