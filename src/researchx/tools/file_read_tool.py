"""File reading tool."""

from __future__ import annotations

from pathlib import Path
import hashlib
from contextlib import nullcontext

from pydantic import BaseModel, Field

from researchx.tools.base import BaseTool, ToolExecutionContext, ToolResult
from researchx.state.errors import ResearchError


class FileReadToolInput(BaseModel):
    """Arguments for the file read tool."""

    path: str = Field(description="要读取的文件路径")
    offset: int = Field(default=0, ge=0, description="从零开始计数的起始行号")
    limit: int = Field(default=200, ge=1, le=2000, description="返回的行数")


class FileReadTool(BaseTool[FileReadToolInput]):
    """Read a UTF-8 text file with line numbers."""

    name = "read_file"
    contract = {
        "parallelism": "resources",
        "name": "read_file",
        "source": "builtin",
        "effect": "read_only",
        "required_capabilities": ("filesystem.read",),
        "resources_read": ("path",),
    }
    description = "读取研究工作区中的文本文件。"
    input_model = FileReadToolInput

    def is_read_only(self, arguments: FileReadToolInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: FileReadToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        try:
            return await self._read(arguments, context)
        except (ResearchError, OSError, UnicodeError, ValueError) as exc:
            return ToolResult(output=str(exc), is_error=True)

    async def _read(
        self, arguments: FileReadToolInput, context: ToolExecutionContext
    ) -> ToolResult:
        from researchx.skills.resources import validate_skill_file_path, selected_skill_resource

        candidate = Path(arguments.path).expanduser()
        candidate = candidate if candidate.is_absolute() else context.cwd / candidate
        validate_skill_file_path(candidate)
        resource = selected_skill_resource(context, candidate)
        path = resource if resource else (await context.resolve_path(arguments.path))

        from researchx.sandbox.session import is_docker_sandbox_active

        if is_docker_sandbox_active() and resource is None:
            from researchx.sandbox.path_validator import validate_sandbox_path

            allowed, reason = validate_sandbox_path(path, context.cwd)
            if not allowed:
                return ToolResult(output=f"Sandbox: {reason}", is_error=True)

        if not path.exists():
            return ToolResult(output=f"File not found: {path}", is_error=True)
        if path.is_dir():
            return ToolResult(output=f"Cannot read directory: {path}", is_error=True)

        async with nullcontext(resource) if resource else context.file_lock(arguments.path) as path:
            raw = path.read_bytes()
        if b"\x00" in raw:
            return ToolResult(output=f"Binary file cannot be read as text: {path}", is_error=True)

        project_mode = (await context.workspace_runtime()) is not None
        runtime = await context.workspace_runtime()
        memory_file = bool(
            runtime
            and path
            == (
                await runtime.resolve_workspace(
                    runtime.repository._project((await runtime.store.load())).id
                )
            )
            / "MEMORY.md"
        )
        text = raw.decode("utf-8", errors="strict" if project_mode else "replace")
        lines = text.splitlines()
        selected = lines[arguments.offset : arguments.offset + arguments.limit]
        numbered = [
            f"{arguments.offset + index + 1:>6}\t{line}" for index, line in enumerate(selected)
        ]
        digest = hashlib.sha256(raw).hexdigest()
        from researchx.state.models import SourceUsage

        usage = SourceUsage()
        policy_store = context.metadata.get("research_store")
        workspace_runtime = context.metadata.get("research_workspace_runtime")
        if policy_store is None and workspace_runtime is not None:
            policy_store = workspace_runtime.store
        if policy_store is not None:
            memory = await policy_store.load()
            # Reuse only a classification for these exact bytes at this exact locator.
            # A changed file must be classified again; path names never confer permission.
            for source in memory.sources.values():
                if (
                    source.kind == "file"
                    and source.locator == str(path)
                    and source.content_hash == digest
                ):
                    usage = source.usage
                    break
            from researchx.state.source_policy import may_enter_external_model, may_export
            from researchx.state.dispatch_audit import authorised_candidate_file

            if await authorised_candidate_file(policy_store, path, digest):
                usage = SourceUsage(
                    category="internal",
                    provider="generated_candidate",
                    licence_status="confirmed",
                    model_use_allowed=True,
                    completeness="fragment",
                )

            if memory_file:
                # The workspace memory is operational context explicitly managed by
                # the main agent, not a newly ingested external document.
                usage = SourceUsage(
                    category="internal",
                    provider="workspace_context",
                    licence_status="confirmed",
                    model_use_allowed=True,
                    completeness="fragment",
                )
            elif resource:
                # The selected Skill resource resolver already checks the
                # approved plugin root and symlinks. Instructions are executable
                # operational context, not authority to classify research data.
                usage = SourceUsage(
                    category="internal",
                    provider="approved_skill_resource",
                    licence_status="confirmed",
                    model_use_allowed=True,
                    completeness="fragment",
                )
            for artifact in memory.artifacts.values():
                if artifact.content_hash != digest or artifact.stale or not artifact.evidence_ids:
                    continue
                inputs = [
                    memory.sources[memory.evidence_pool[key].source_id].usage
                    for key in artifact.evidence_ids
                    if key in memory.evidence_pool
                ]
                if len(inputs) == len(artifact.evidence_ids) and all(
                    may_enter_external_model(item) for item in inputs
                ):
                    usage = SourceUsage(
                        category="internal",
                        provider="derived_research_artifact",
                        licence_status="confirmed",
                        model_use_allowed=True,
                        internal_export_allowed=all(may_export(item) for item in inputs),
                        completeness="full",
                    )
                    break
        output = "\n".join(numbered) if numbered else f"(no content in selected range for {path})"
        if project_mode:
            output += f"\n\nexpected_sha256: {digest} (whole file; use for write_file overwrite)"
        return ToolResult(
            output=output,
            metadata={
                **(
                    {"context_component": "memory"}
                    if memory_file
                    else {"context_component": "dynamic_context"}
                    if resource
                    else {}
                ),
                "content_sha256": digest,
                "research_source_specs": [
                    {
                        "kind": "file",
                        "__host_usage": usage.model_dump(mode="json"),
                        "title": path.name,
                        "locator": f"{path}:{arguments.offset + 1}-{arguments.offset + len(selected)}",
                        "content": "\n".join(numbered),
                        "fragment": (project_mode and path.name == "MEMORY.md")
                        or arguments.offset > 0
                        or arguments.offset + len(selected) < len(lines),
                    }
                ],
            },
        )


def _resolve_path(base: Path, candidate: str) -> Path:
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()
