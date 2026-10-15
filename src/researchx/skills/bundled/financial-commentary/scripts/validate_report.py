"""Verify report structure, upstream metrics and evidence gaps without calculating."""

if not __package__:
    from researchx.skills.runtime_support import script_package

    __package__ = script_package(__file__)

from .models import ReportResult
from researchx.skills.runtime_support import processing_main
from researchx.skills._shared.reporting import validate_report


def main(argv: list[str] | None = None) -> None:
    processing_main(ReportResult, validate_report, argv)


if __name__ == "__main__":
    main()
