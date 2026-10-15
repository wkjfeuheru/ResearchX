"""Tool for reading skill contents."""

from __future__ import annotations

from pathlib import Path
import sys

from pydantic import BaseModel, Field

from researchx.skills import load_skill_registry
from researchx.skills.resources import RESOURCE_DIRS, resolve_resource
from researchx.tools.base import BaseTool, ToolExecutionContext, ToolResult


class SkillToolInput(BaseModel):
    """Arguments for skill lookup."""

    name: str = Field(description="技能名称")


class SkillTool(BaseTool[SkillToolInput]):
    """Return the content of a loaded skill."""

    name = "skill"
    description = "读取已启用的插件、用户或项目技能并定位其资源。支持文件按需读取，绝不自动执行。"
    input_model = SkillToolInput

    def is_read_only(self, arguments: SkillToolInput) -> bool:
        del arguments
        return True

    async def execute(self, arguments: SkillToolInput, context: ToolExecutionContext) -> ToolResult:
        registry = load_skill_registry(
            context.cwd,
            extra_skill_dirs=context.metadata.get("extra_skill_dirs"),
            settings=context.metadata.get("skill_settings"),
        )
        skill = (
            registry.get(arguments.name)
            or registry.get(arguments.name.lower())
            or registry.get(arguments.name.title())
        )
        if skill is None:
            return ToolResult(output=f"未找到技能：{arguments.name}", is_error=True)
        if skill.metadata.status in {"draft", "retired"}:
            return ToolResult(
                output=f"Skill {skill.name} is {skill.metadata.status} and cannot be executed.",
                is_error=True,
            )
        if skill.disable_model_invocation:
            command_name = skill.command_name or skill.name
            return ToolResult(
                output=f"Skill {command_name} can only be invoked by the user as /{command_name}.",
                is_error=True,
            )
        tools = context.metadata.get("tool_registry")
        if tools is None and skill.metadata.required_tools:
            from researchx.tools import create_research_tool_registry

            tools = create_research_tool_registry()
        if tools is not None:
            missing = [name for name in skill.metadata.required_tools if tools.get(name) is None]
            if missing:
                return ToolResult(
                    output=f"Skill {skill.name} requires unavailable tools: {', '.join(missing)}",
                    is_error=True,
                )
        try:
            content = skill.load_content()
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"无法加载技能 {skill.name}：{exc}", is_error=True)
        paths = [f"技能状态：{skill.metadata.status}", f"Python 解释器：{sys.executable}"]
        if skill.metadata.deprecation:
            paths.append(f"弃用说明：{skill.metadata.deprecation}")
        store = context.metadata.get("research_store")
        if store is not None:
            output = (
                context.cwd / "reports"
                if (await context.workspace_runtime())
                else store.directory / "work"
            )
            paths.append(f"当前会话输出目录：{output}")
        if skill.path:
            paths.append(f"技能入口：{skill.path}")
        if skill.base_dir:
            paths.append(f"资源基础目录：{skill.base_dir}")
            base = Path(skill.base_dir).resolve()
            for directory in RESOURCE_DIRS:
                root = base / directory
                if not root.is_dir() or not root.resolve().is_relative_to(base):
                    continue
                paths.append(f"- {directory}/: {root.resolve()}")
            paths.append(
                "只有当前步骤确实需要时才使用 read_file 和上述绝对路径读取支持文件。"
                "脚本和模板是资源；列出它们不会执行或加载它们。"
            )
        # Validate entry links, without reading any target body or executing scripts.
        import re

        for link in re.findall(r"\]\(([^)]+)\)", content):
            if "://" in link or link.startswith("#"):
                continue
            try:
                resolve_resource(skill.base_dir or ".", link.split("#", 1)[0])
            except ValueError as exc:
                return ToolResult(output=str(exc), is_error=True)
        # Grant read-only resource access only after validating the selected entry.
        shared = getattr(context.metadata.get("query_context"), "tool_metadata", context.metadata)
        if skill.base_dir:
            shared.setdefault("selected_skill_roots", {})[skill.name] = skill.base_dir
        return ToolResult(
            output="\n".join(paths) + "\n\n" + content,
            metadata={
                "context_component": "dynamic_context",
                "skill_entrypoint": skill.path,
                "skill_base_dir": skill.base_dir,
                "skill_metadata": skill.metadata.model_dump(),
            },
        )
