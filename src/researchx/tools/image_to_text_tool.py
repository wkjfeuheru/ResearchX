"""Convert images to text descriptions using a multimodal model.

This tool acts as a bridge for pure-text models: when the user attaches an
image but the active model cannot process images natively, the agent loop
(or the model itself) can invoke this tool to obtain a text/JSON description
of the image via a separately configured vision-capable model.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from typing import Any

from pydantic import BaseModel, Field

from researchx.api.openai_client import OpenAICompatibleClient
from researchx.tools.base import BaseTool, ToolExecutionContext, ToolResult

log = logging.getLogger(__name__)

# Default system prompt for image description.
_DEFAULT_VISION_PROMPT = (
    "你是图像描述助手。请详细描述图像，包括文字、物体、人物、颜色、布局和上下文。"
    "如果图像包含代码、UI 截图、图表或数据可视化，请准确描述，"
    "让只能处理文本的 AI 模型也能理解其内容。"
)


class ImageToTextToolInput(BaseModel):
    """Arguments for converting an image to text."""

    image_data: str | None = Field(
        default=None,
        description="Base64 编码的图像数据。请提供 image_data 或 image_path。",
    )
    image_path: str | None = Field(
        default=None,
        description="图像的本地文件路径。请提供 image_data 或 image_path。",
    )
    prompt: str = Field(
        default=_DEFAULT_VISION_PROMPT,
        description="描述图像的自定义说明；默认使用通用图像描述提示词。",
    )
    media_type: str = Field(
        default="image/png",
        description="图像的 MIME 类型（例如 image/png、image/jpeg、image/webp）。"
        "仅在提供 image_data 时使用。",
    )
    max_tokens: int = Field(
        default=2048,
        ge=256,
        le=16384,
        description="视觉模型回答允许使用的最大 Token 数。",
    )


class ImageToTextTool(BaseTool[ImageToTextToolInput]):
    """Use a multimodal model to describe an image and return text."""

    name = "image_to_text"
    contract = {
        "name": "image_to_text",
        "source": "builtin",
        "effect": "model_call",
        "required_capabilities": ("model.call", "filesystem.read"),
        "resources_read": ("image_path",),
        "resources_write": ("model:image_to_text",),
        "parallelism": "resources",
    }
    description = (
        "使用支持视觉的模型将图像转换为详细的文字描述。"
        "当当前模型不支持图像输入、但你需要理解图像内容时使用。"
    )
    input_model = ImageToTextToolInput

    async def execute(
        self, arguments: ImageToTextToolInput, context: ToolExecutionContext
    ) -> ToolResult:
        # 1. Resolve image data
        try:
            image_data, media_type = await self._resolve_image(arguments, context)
        except (ValueError, OSError) as exc:
            return ToolResult(output=str(exc), is_error=True)
        if image_data is None:
            return ToolResult(
                output="image_to_text failed: provide either image_data (base64) or image_path",
                is_error=True,
            )
        policy_store = context.metadata.get("research_store")
        workspace_runtime = context.metadata.get("research_workspace_runtime")
        if policy_store is None and workspace_runtime is not None:
            policy_store = workspace_runtime.store
        if policy_store is not None:
            from researchx.state.source_policy import image_use_allowed, request_policy_view

            try:
                digest = hashlib.sha256(base64.b64decode(image_data, validate=True)).hexdigest()
            except (ValueError, binascii.Error):
                return ToolResult(
                    output="图像数据编码无效。", is_error=True, error_code="invalid_input"
                )
            policy_view, approved_artifact_hashes = await request_policy_view(
                policy_store.workspace_id, policy_store.session_id, [], {digest}
            )
            if not image_use_allowed(policy_view, digest, approved_artifact_hashes):
                return ToolResult(
                    output="图像来源使用范围尚未确认，不能发送至外部视觉模型。",
                    is_error=True,
                    error_code="source_use_not_authorized",
                )

        # 2. Get vision model config from context metadata
        vision_config = context.metadata.get("vision_model_config", {})
        if not isinstance(vision_config, dict):
            vision_config = {}

        model = vision_config.get("model", "")
        api_key = vision_config.get("api_key", "")
        base_url = vision_config.get("base_url", "")

        if not model or not api_key:
            log.warning(
                "image_to_text: vision model not configured. "
                "Set vision.model and vision.api_key in settings."
            )
            return ToolResult(
                output=(
                    "image_to_text failed: vision model is not configured. "
                    "Please set vision.model and vision.api_key in your settings, "
                    "or configure the RESEARCHX_VISION_MODEL and "
                    "RESEARCHX_VISION_API_KEY environment variables."
                ),
                is_error=True,
            )

        # 3. Call the vision model
        try:
            description = await self._call_vision_model(
                image_data=image_data,
                media_type=media_type or arguments.media_type,
                prompt=arguments.prompt,
                model=model,
                api_key=api_key,
                base_url=base_url,
                max_tokens=arguments.max_tokens,
                context_window_tokens=int(vision_config["context_window_tokens"])
                if vision_config.get("context_window_tokens")
                else None,
            )
        except Exception as exc:
            log.exception("image_to_text: vision model call failed")
            return ToolResult(
                output=f"image_to_text failed: vision model error: {exc}",
                is_error=True,
            )

        return ToolResult(output=(f"[Image description via {model}]\n\n{description}"))

    def is_read_only(self, arguments: BaseModel) -> bool:
        del arguments
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _resolve_image(
        arguments: ImageToTextToolInput,
        context: ToolExecutionContext,
    ) -> tuple[str | None, str | None]:
        """Resolve image data from either base64 string or file path."""
        if arguments.image_data:
            return arguments.image_data, arguments.media_type

        if arguments.image_path:
            path = await context.resolve_path(arguments.image_path)

            if not path.exists():
                log.warning("image_to_text: image not found at %s", path)
                return None, None

            try:
                raw = path.read_bytes()
                data = base64.b64encode(raw).decode("ascii")
            except OSError as exc:
                log.warning("image_to_text: failed to read %s: %s", path, exc)
                return None, None

            # Guess media type from extension
            ext = path.suffix.lower()
            media_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".bmp": "image/bmp",
                ".svg": "image/svg+xml",
            }.get(ext, "image/png")

            return data, media_type

        return None, None

    @staticmethod
    async def _call_vision_model(
        *,
        image_data: str,
        media_type: str,
        prompt: str,
        model: str,
        api_key: str,
        base_url: str,
        max_tokens: int,
        context_window_tokens: int | None = None,
    ) -> str:
        """Call the vision model via OpenAI-compatible API."""
        client = OpenAICompatibleClient(
            api_key=api_key,
            base_url=base_url or None,
        )

        from researchx.api.client import ApiMessageRequest
        from researchx.engine.messages import (
            ConversationMessage,
            ImageBlock,
            TextBlock,
        )

        # Build a user message with the image
        user_content: list[Any] = [TextBlock(text=prompt)]
        user_content.append(
            ImageBlock(
                media_type=media_type,
                data=image_data,
            )
        )
        user_message = ConversationMessage(role="user", content=user_content)

        # Stream the response and collect text
        collected_text = ""
        async for event in client.stream_message(
            ApiMessageRequest(
                model=model,
                messages=[user_message],
                system_prompt="",
                max_tokens=max_tokens,
                tools=[],
                context_window_tokens=context_window_tokens,
            )
        ):
            from researchx.api.client import ApiTextDeltaEvent, ApiMessageCompleteEvent

            if isinstance(event, ApiTextDeltaEvent):
                collected_text += event.text
            elif isinstance(event, ApiMessageCompleteEvent):
                # Also grab any text from the final message
                text = event.message.text
                if text and text not in collected_text:
                    collected_text = text

        return collected_text.strip() or "(no description returned)"
