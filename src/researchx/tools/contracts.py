"""Internal execution contracts; provider tool schemas remain unchanged."""

from __future__ import annotations

import json
from typing import Any, Literal, TYPE_CHECKING
from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from researchx.tools.base import BaseTool

Effect = Literal["read_only", "local_write", "external_write", "model_call", "mixed", "unknown"]
RetryMode = Literal["never", "idempotent", "idempotency_key", "reconcile_before_retry"]
ResultStatus = Literal[
    "success", "failed", "partial", "uncertain", "cancelled", "denied", "blocked"
]


class ToolContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")
    version: str = Field(default="1", min_length=1)
    description: str = ""
    source: Literal["builtin", "mcp", "legacy"] = "legacy"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    output_model: type[BaseModel] | None = Field(default=None, exclude=True)
    required_capabilities: frozenset[str] = frozenset()
    effect: Effect = "unknown"
    resources_read: tuple[str, ...] = ()
    resources_write: tuple[str, ...] = ()
    retry_mode: RetryMode = "never"
    max_attempts: int = Field(default=1, ge=1, le=3)
    idempotency_key_supported: bool = False
    timeout_seconds: float = Field(default=600, gt=0, le=3600)
    cancellable: bool = True
    max_output_chars: int = Field(default=30000, ge=256, le=1000000)
    parallelism: Literal["serial", "resources"] = "serial"
    result_statuses: tuple[ResultStatus, ...] = (
        "success",
        "failed",
        "partial",
        "uncertain",
        "cancelled",
        "denied",
        "blocked",
    )
    result_semantics: str = (
        "Errors after possible writes are uncertain unless absence of effects is proven."
    )
    observability: Literal["digest_only"] = "digest_only"

    @model_validator(mode="after")
    def validate_semantics(self) -> ToolContract:
        if self.effect in {"unknown", "mixed", "model_call"} and self.retry_mode != "never":
            raise ValueError("Unknown/model operations cannot enable business retries")
        if self.retry_mode == "never" and self.max_attempts != 1:
            raise ValueError("retry_mode=never requires max_attempts=1")
        if self.retry_mode == "idempotency_key" and not self.idempotency_key_supported:
            raise ValueError("idempotency_key requires an adapter-certified remote guarantee")
        if self.effect == "read_only" and self.resources_write:
            raise ValueError("read_only contracts cannot declare writes")
        if self.parallelism == "resources" and not (self.resources_read or self.resources_write):
            raise ValueError("resource parallelism requires resource declarations")
        json.dumps(self.input_schema)
        return self


def resolve_contract(tool: BaseTool[Any], arguments: BaseModel | None = None) -> ToolContract:
    declared = tool.execution_contract(arguments)
    schema = tool.input_schema()
    if declared is not None:
        contract = ToolContract.model_validate(declared)
        if contract.retry_mode == "idempotency_key":
            from researchx.tools.base import BaseTool

            if type(tool).execute_with_idempotency_key is BaseTool.execute_with_idempotency_key:
                raise ValueError("idempotency_key requires execute_with_idempotency_key adapter")
        if contract.name != tool.name:
            raise ValueError("Tool contract name does not match registered name")
        if contract.input_schema and contract.input_schema != schema:
            raise ValueError("Contract input schema must derive from input_schema")
        return contract.model_copy(update={"input_schema": schema})
    return ToolContract(
        name=tool.name,
        description=tool.description,
        input_schema=schema,
        effect="read_only" if arguments is not None and tool.is_read_only(arguments) else "unknown",
    )


RETIRED_TOOL_NAMES = frozenset(
    {"notebook_edit", "config", "mcp_auth", "image_generation", "sleep", "investigate_conflict"}
)
