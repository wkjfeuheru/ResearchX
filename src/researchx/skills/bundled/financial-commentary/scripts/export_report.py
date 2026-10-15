"""Export a checked report using this Skill's template."""

if not __package__:
    from researchx.skills.runtime_support import script_package

    __package__ = script_package(__file__)

from pathlib import Path
from .models import ReportResult
from researchx.skills.runtime_support import export_main
from researchx.skills._shared.reporting import export_report as _export


def export_result(
    result: ReportResult,
    directory: Path,
    session_directory: Path | None = None,
    task_id: str | None = None,
) -> dict[str, object]:
    return _export(
        result,
        directory,
        session_directory,
        task_id,
        template=Path(__file__).parents[1] / "templates/report.md",
    )


def main(argv: list[str] | None = None) -> None:
    export_main(ReportResult, export_result, argv)


if __name__ == "__main__":
    main()
