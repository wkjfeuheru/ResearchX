"""File-oriented CLI helpers shared by independently implemented skill scripts."""

from __future__ import annotations

from typing import TypeVar, Callable
from pydantic import BaseModel
import argparse
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from researchx.storage.filesystem import atomic_write_text

T = TypeVar("T")
ResultT = TypeVar("ResultT", bound=BaseModel)


def script_package(filename: str) -> str:
    """Resolve an installed script's package without depending on the working directory."""
    # ResearchX itself can be a namespace package (and have no __file__).
    root = Path(__file__).resolve().parents[1]
    directory = Path(filename).resolve().parent
    if directory.is_relative_to(root):
        relative = directory.relative_to(root)
        package = ".".join(("researchx", *relative.parts))
        importlib.import_module(package)
        return package
    # User/workspace Skill copies also need isolated sibling imports.
    package = "_researchx_skill_scripts_" + hashlib.sha256(str(directory).encode()).hexdigest()
    if package not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            package, directory / "__init__.py", submodule_search_locations=[str(directory)]
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load skill scripts: {directory}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(package, None)
            raise
    return package


def guarded(operation: Callable[[], T]) -> T:
    """Return machine-readable, redacted errors from script operations."""
    try:
        return operation()
    except Exception as exc:
        from researchx.security.redaction import memory_credentials

        message = str(exc)
        for secret in memory_credentials():
            if secret:
                message = message.replace(secret, "[hidden]")
        print(
            json.dumps({"status": "failed", "error": message[:4000]}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def processing_main(
    result_type: type[ResultT],
    processor: Callable[[ResultT], ResultT],
    argv: list[str] | None = None,
) -> None:
    """Supply JSON I/O to one skill's own validation and computation function."""
    parser = argparse.ArgumentParser(description=processor.__doc__ or processor.__name__)
    parser.add_argument("--schema", action="store_true", help="Print this skill's JSON schema")
    parser.add_argument("--input", help="Model-extracted structured JSON")
    parser.add_argument("--output", help="Computed structured JSON path")
    args = parser.parse_args(argv)
    if args.schema:
        if args.input or args.output:
            parser.error("--schema cannot be combined with --input or --output")
        print(json.dumps(result_type.model_json_schema(), ensure_ascii=False, indent=2))
        return
    if not args.input or not args.output:
        parser.error("--input and --output are required unless --schema is used")

    def process() -> None:
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
        from researchx.contracts.research import require_production_contract

        require_production_contract(raw)
        result = result_type.model_validate(raw)
        result = processor(result)
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output, result.model_dump_json(indent=2))
        data = result.model_dump()
        print(
            json.dumps(
                {"status": data["status"], "gaps": data["gaps"], "result": str(output)},
                ensure_ascii=False,
            )
        )

    guarded(process)


def export_main(
    result_type: type[ResultT],
    exporter: Callable[[ResultT, Path, Path | None, str | None], dict[str, object]],
    argv: list[str] | None = None,
) -> None:
    """Supply artifact CLI arguments to a skill-owned report exporter."""
    parser = argparse.ArgumentParser(description="Export an already computed skill result")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--session-dir", default=os.environ.get("RESEARCHX_RESEARCH_SESSION_DIR"))
    parser.add_argument("--task-id", default=os.environ.get("RESEARCHX_RESEARCH_TASK_ID"))
    args = parser.parse_args(argv)

    def export() -> None:
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
        from researchx.contracts.research import require_production_contract

        require_production_contract(raw)
        result = result_type.model_validate(raw)
        exported = exporter(
            result,
            Path(args.output_dir),
            Path(args.session_dir) if args.session_dir else None,
            args.task_id,
        )
        print(json.dumps(exported, ensure_ascii=False))

    guarded(export)
