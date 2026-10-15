"""Contract for financial-commentary."""

from typing import Literal
from researchx.skills._shared.reporting import ReportDocument


class ReportResult(ReportDocument):
    kind: Literal["financial-commentary"] = "financial-commentary"
