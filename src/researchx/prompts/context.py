"""Higher-level system prompt assembly."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from researchx.config.settings import Settings
from researchx.permissions.modes import PermissionMode
from researchx.prompts.system_prompt import get_base_system_prompt, _format_environment_section
from researchx.prompts.environment import get_environment_info
from researchx.skills.loader import load_skill_registry
from typing_extensions import TypedDict, Unpack
from researchx.engine.messages import ContextSpan


def _build_skills_section(
    cwd: str | Path,
    *,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Build a system prompt section listing available skills."""
    registry = load_skill_registry(
        cwd,
        extra_skill_dirs=extra_skill_dirs,
        settings=settings,
    )
    skills = [
        skill
        for skill in registry.list_skills()
        if not skill.disable_model_invocation and skill.metadata.status in {"active", "deprecated"}
    ]
    if not skills:
        return None
    lines = [
        "# 可用技能",
        "",
        "以下技能可通过 `skill` 工具使用。"
        '用户请求与某项技能匹配时，先用 `skill(name="<skill_name>")` '
        "加载详细说明，再继续处理。"
        "用户可以在 Web 工作区启用技能。",
        "",
    ]
    for skill in sorted(skills, key=lambda item: item.command_name or item.name):
        command_name = skill.command_name or skill.name
        display = f" ({skill.display_name})" if skill.display_name else ""
        lines.append(f"- **{command_name}**{display}: {skill.description}")
        if skill.metadata.status == "deprecated":
            lines.append(f"  已弃用：{skill.metadata.deprecation or '未指定替代方案'}")
    return "\n".join(lines)


def _build_permission_mode_section(settings: Settings) -> str:
    """Build current permission-mode guidance for the model."""
    mode = settings.permission.mode
    if mode == PermissionMode.PLAN:
        guidance = (
            "当前已启用计划模式。本会话仅用于只读规划和分析。"
            "除非用户退出计划模式，否则不要调用文件写入、编辑、安装软件包、"
            "改变状态的 Shell 命令或创建任务等会产生修改的工具。"
        )
    elif mode == PermissionMode.FULL_AUTO:
        guidance = (
            "当前已启用全自动权限模式。用户请求确有需要时可以使用会产生修改的工具，"
            "但仍要限定变更范围并保持操作有明确目的。"
        )
    else:
        guidance = (
            "当前已启用默认权限模式。只读工具可以直接运行；会产生修改的工具可能需要用户明确批准。"
        )
    return f"# 当前权限模式\n{guidance}"


@dataclass(frozen=True)
class RuntimePrompt:
    system_prompt: str
    runtime_context: str
    runtime_context_manifest: list[ContextSpan] | None = None


def build_runtime_prompt(
    settings: Settings,
    *,
    cwd: str | Path,
    latest_user_prompt: str | None = None,
    extra_skill_dirs: Iterable[str | Path] | None = None,
) -> RuntimePrompt:
    """Keep instruction/catalog prefixes stable and snapshot per-turn reference state."""
    sections = [settings.system_prompt or get_base_system_prompt()]

    dynamic = [_format_environment_section(get_environment_info(cwd=str(cwd)))]
    dynamic.append(
        "<permission_mode>\n" + _build_permission_mode_section(settings) + "\n</permission_mode>"
    )

    if settings.fast_mode:
        dynamic.append(
            "# 会话模式\n当前已启用快速模式。优先使用简洁回答、最少的工具调用和更快的推进速度，不必穷尽探索。"
        )

    dynamic.append(
        "# 推理设置\n"
        f"- 推理强度：{settings.effort}\n"
        f"- 轮次：{settings.passes}\n"
        "在完成任务的前提下，根据这些设置调整分析深度和迭代次数。"
    )

    skills_section = _build_skills_section(
        cwd,
        extra_skill_dirs=extra_skill_dirs,
        settings=settings,
    )
    if skills_section:
        sections.append(skills_section)

    if settings.research.context.auto_inject:
        from researchx.state.prompt import RESEARCH_MEMORY_PROMPT

        sections.append(RESEARCH_MEMORY_PROMPT)

    from researchx.services.context.sources import ContextSnapshot

    snapshot = ContextSnapshot.join(
        [
            (text, "dynamic_context" if index == 0 else "system_prompt", "runtime", index == 0)
            for index, text in enumerate(dynamic)
            if text.strip()
        ]
    )
    return RuntimePrompt(
        system_prompt="\n\n".join(section for section in sections if section.strip()),
        runtime_context=snapshot.text,
        runtime_context_manifest=snapshot.manifest,
    )


class PromptOptions(TypedDict, total=False):
    cwd: str
    latest_user_prompt: str | None
    extra_skill_dirs: Iterable[str | Path] | None


def build_runtime_system_prompt(settings: Settings, **kwargs: Unpack[PromptOptions]) -> str:
    """Legacy combined prompt for previews and external integrations."""
    prompt = build_runtime_prompt(settings, **kwargs)
    return prompt.system_prompt + "\n\n" + prompt.runtime_context
