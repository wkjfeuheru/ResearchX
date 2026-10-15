"""Contract for industry-deep-dive."""

from typing import Literal
from researchx.skills._shared.reporting import ReportDocument


class ReportResult(ReportDocument):
    kind: Literal["industry-deep-dive"] = "industry-deep-dive"
