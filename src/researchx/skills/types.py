"""Skill data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class SkillMetadata(BaseModel):
    """Portable lifecycle and capability declarations; permissions remain enforced by tools."""

    skill_id: str = ""
    version: str = "0.1.0"
    owner: str = "未声明"
    permissions: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    optional_tools: list[str] = Field(default_factory=list)
    compatible_models: list[str] = Field(default_factory=lambda: ["text", "tool_calling"])
    scope: str = "current_conversation"
    status: Literal["draft", "active", "deprecated", "retired"] = "active"
    published_at: str | None = None
    deprecation: dict[str, str] | None = None
    content_hash: str = ""


@dataclass(frozen=True)
class SkillDefinition:
    """Discovered metadata. Entry content is loaded explicitly after selection."""

    name: str
    description: str
    content: str | None
    source: str
    path: str | None = None
    base_dir: str | None = None
    command_name: str | None = None
    display_name: str | None = None
    aliases: tuple[str, ...] = ()
    user_invocable: bool = True
    disable_model_invocation: bool = False
    model: str | None = None
    argument_hint: str | None = None
    metadata: SkillMetadata = field(default_factory=SkillMetadata)
    enabled: bool = True
    approved_root: str | None = None

    @property
    def id(self) -> str:
        from hashlib import sha256

        location = (
            self.command_name or self.name if self.source == "builtin" else self.path or self.name
        )
        return self.source + "_" + sha256(location.encode()).hexdigest()[:24]

    def load_content(self) -> str:
        """Read only this entrypoint, never its supporting resources."""
        if self.content is not None:
            return self.content
        if not self.path or not self.base_dir:
            return ""
        if self.approved_root and not Path(self.path).resolve().is_relative_to(
            Path(self.approved_root)
        ):
            raise ValueError("Skill entrypoint escaped its originally approved root")
        from researchx.skills.resources import resolve_resource

        return resolve_resource(self.base_dir, self.path).read_text(encoding="utf-8")
