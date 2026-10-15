"""Profiles, credentials and Skills shared with the CLI."""

from __future__ import annotations

from uuid import uuid4

from fastapi import HTTPException

from researchx.auth.manager import AuthManager
from researchx.config import Settings, load_settings, save_settings
from researchx.config.settings import ProviderProfile, builtin_provider_profile_names
from researchx.web.models import ModelInput


from typing_extensions import TypedDict


class ModelCard(TypedDict):
    id: str
    label: str
    api_format: str
    model: str
    base_url: str | None
    context_window_tokens: int | None
    auto_compact_threshold_tokens: int | None
    configured: bool
    active: bool
    supported: bool
    editable: bool
    auth_source: str
    builtin: bool


class ModelCatalog(TypedDict):
    active_profile: str
    items: list[ModelCard]


def profile_settings(profile_id: str) -> Settings:
    settings = load_settings()
    profile = settings.merged_profiles().get(profile_id)
    if profile is None:
        raise HTTPException(404, "模型配置不存在")
    if profile.api_format not in {"openai", "openai_compat", "anthropic", "copilot"}:
        raise HTTPException(400, "此模型接口格式不受支持")
    return settings.model_copy(update={"active_profile": profile_id}).materialize_active_profile()


def models_list() -> ModelCatalog:
    manager = AuthManager()
    active = manager.get_active_profile()
    builtins = builtin_provider_profile_names()
    items: list[ModelCard] = []
    for name, profile in manager.list_profiles().items():
        supported = profile.api_format in {"openai", "openai_compat", "anthropic", "copilot"}
        editable = profile.auth_source not in {
            "codex_subscription",
            "claude_subscription",
            "copilot_oauth",
        }
        configured = False
        if supported:
            try:
                if profile.auth_source == "copilot_oauth":
                    from researchx.api.copilot_auth import load_copilot_auth

                    configured = load_copilot_auth() is not None
                else:
                    configured = bool(profile_settings(name).resolve_auth().value)
            except (ValueError, HTTPException):
                pass
        items.append(
            {
                "id": name,
                "label": profile.label,
                "api_format": profile.api_format,
                "model": profile.resolved_model,
                "base_url": profile.base_url,
                "context_window_tokens": profile.context_window_tokens,
                "auto_compact_threshold_tokens": profile.auto_compact_threshold_tokens,
                "configured": configured,
                "active": name == active,
                "supported": supported,
                "editable": editable,
                "auth_source": profile.auth_source,
                "builtin": name in builtins,
            }
        )
    return {"active_profile": active, "items": items}


def save_model(data: ModelInput, profile_id: str | None = None) -> str:
    manager = AuthManager()
    old = None
    if profile_id:
        profile_settings(profile_id)
        old = manager.list_profiles()[profile_id]
        if old.auth_source in {"codex_subscription", "claude_subscription", "copilot_oauth"}:
            raise HTTPException(400, "订阅认证配置请使用 CLI 管理；网页可选择并测试连接")
    else:
        profile_id = f"web-{uuid4().hex[:12]}"
    # Preserve an existing slot when editing; new API configs have independent credentials.
    profile = ProviderProfile(
        label=data.label,
        provider=data.api_format,
        api_format=data.api_format,
        auth_source=f"{data.api_format}_api_key",
        default_model=data.model,
        last_model=data.model,
        base_url=data.base_url,
        credential_slot=old.credential_slot if old else profile_id,
        context_window_tokens=(
            data.context_window_tokens
            if "context_window_tokens" in data.model_fields_set
            else old.context_window_tokens
            if old and old.resolved_model == data.model
            else None
        ),
        auto_compact_threshold_tokens=(
            data.auto_compact_threshold_tokens
            if "auto_compact_threshold_tokens" in data.model_fields_set
            else old.auto_compact_threshold_tokens
            if old
            else None
        ),
    )
    manager.upsert_profile(profile_id, profile)
    if data.api_key and data.api_key.get_secret_value().strip():
        manager.store_profile_credential(
            profile_id, "api_key", data.api_key.get_secret_value().strip()
        )
    return profile_id


def skills_list(cwd: str) -> list[dict[str, object]]:
    from researchx.skills.loader import load_skill_registry

    return [
        {
            "id": skill.id,
            "name": skill.command_name or skill.name,
            "label": skill.display_name or skill.command_name or skill.name,
            "source": skill.source,
            "description": skill.description,
            "enabled": skill.enabled,
            "metadata": skill.metadata.model_dump(),
            "version": skill.metadata.version,
        }
        for skill in load_skill_registry(
            cwd, settings=load_settings(), include_disabled=True
        ).list_skills()
    ]


def toggle_skill(cwd: str, skill_id: str, enabled: bool) -> None:
    if not any(skill["id"] == skill_id for skill in skills_list(cwd)):
        raise HTTPException(404, "技能不存在")
    settings = load_settings()
    save_settings(
        settings.model_copy(
            update={"enabled_skills": {**settings.enabled_skills, skill_id: enabled}}
        )
    )


def skill_detail(cwd: str, skill_id: str) -> dict[str, object]:
    from researchx.skills.loader import load_skill_registry

    skill = next(
        (
            s
            for s in load_skill_registry(
                cwd, settings=load_settings(), include_disabled=True
            ).list_skills()
            if s.id == skill_id
        ),
        None,
    )
    if skill is None:
        raise HTTPException(404, "技能不存在")
    try:
        return {
            "id": skill.id,
            "name": skill.command_name or skill.name,
            "content": skill.load_content(),
        }
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
