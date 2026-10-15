"""PostgreSQL business records. JSONB belongs to individual records, not an entire memory."""

from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

workspaces = Table(
    "workspaces",
    metadata,
    Column("workspace_id", String(64), primary_key=True),
    Column("canonical_path", Text, nullable=False, unique=True),
    Column("updated_at", Float, nullable=False),
)

research_sessions = Table(
    "research_sessions",
    metadata,
    Column("workspace_id", String(64), primary_key=True),
    Column("session_id", String(128), primary_key=True),
    Column("revision", BigInteger, nullable=False, server_default="0"),
    Column("schema_version", Integer, nullable=False, server_default="1"),
    Column("current_context_id", Text),
    Column("research_state", JSONB, nullable=False, server_default="{}"),
    Column("updated_at", Float, nullable=False),
    ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"]),
    CheckConstraint("revision >= 0"),
)


def scoped_table(name: str, *columns: Column[Any]) -> Table:
    return Table(
        name,
        metadata,
        Column("workspace_id", String(64), primary_key=True),
        Column("session_id", String(128), primary_key=True),
        *columns,
        ForeignKeyConstraint(
            ["workspace_id", "session_id"],
            ["research_sessions.workspace_id", "research_sessions.session_id"],
            ondelete="CASCADE",
        ),
    )


conversation_sessions = scoped_table(
    "conversation_sessions",
    Column("channel", String(16), nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("model", Text, nullable=False),
    Column("summary", Text, nullable=False),
    Column("message_count", Integer, nullable=False),
    Column("payload", JSONB, nullable=False),
)
Index(
    "ix_conversations_workspace_updated",
    conversation_sessions.c.workspace_id,
    conversation_sessions.c.updated_at,
)
conversation_messages = scoped_table(
    "conversation_messages",
    Column("sequence", Integer, primary_key=True),
    Column("payload", JSONB, nullable=False),
)

# Common identity/scope columns; individual payloads retain the Pydantic serialization.
ENTITY_FIELDS = {
    "task_context": "research_contexts",
    "sources": "research_sources",
    "plans": "research_plans",
    "evidence_pool": "research_evidence",
    "reasoning_chain": "research_reasoning_steps",
    "conclusions": "research_conclusions",
    "conflicts": "research_conflicts",
    "arbitrations": "research_arbitrations",
    "project": "research_projects",
    "objectives": "research_objectives",
    "artifacts": "research_artifacts",
    "executions": "research_executions",
    "answers": "research_answers",
    "pending_steers": "research_pending_steers",
}
entities = {}
for field, name in ENTITY_FIELDS.items():
    table = scoped_table(
        name,
        Column("record_id", Text, primary_key=True),
        Column("position", Integer, nullable=False),
        Column("status", Text),
        Column("revision", Integer),
        Column("project_id", Text),
        Column("plan_id", Text),
        Column("task_id", Text),
        Column("content_hash", String(64)),
        Column("payload", JSONB, nullable=False),
    )
    entities[field] = table
research_tasks = scoped_table(
    "research_tasks",
    Column("plan_id", Text, primary_key=True),
    Column("record_id", Text, primary_key=True),
    Column("position", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("payload", JSONB, nullable=False),
)
research_tasks.append_constraint(
    ForeignKeyConstraint(
        ["workspace_id", "session_id", "plan_id"],
        ["research_plans.workspace_id", "research_plans.session_id", "research_plans.record_id"],
        ondelete="CASCADE",
    )
)
Index(
    "ix_tasks_status",
    research_tasks.c.workspace_id,
    research_tasks.c.session_id,
    research_tasks.c.status,
)
research_receipts = scoped_table(
    "research_operation_receipts",
    Column("operation_id", Text, primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("receipt", JSONB, nullable=False),
)
research_history = scoped_table(
    "research_history",
    Column("sequence", BigInteger, primary_key=True),
    Column("revision", BigInteger, nullable=False),
    Column("action", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
)
content_objects = Table(
    "content_objects",
    metadata,
    Column("workspace_id", String(64), primary_key=True),
    Column("content_hash", String(64), primary_key=True),
    Column("object_key", Text, nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("media_type", Text, nullable=False),
    ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"]),
    CheckConstraint("size >= 0"),
)
for field in ("sources", "artifacts"):
    entities[field].append_constraint(
        ForeignKeyConstraint(
            ["workspace_id", "content_hash"],
            ["content_objects.workspace_id", "content_objects.content_hash"],
        )
    )

file_records = scoped_table(
    "session_files",
    Column("record_id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("payload", JSONB, nullable=False),
)
file_records.append_constraint(
    ForeignKeyConstraint(
        ["workspace_id", "content_hash"],
        ["content_objects.workspace_id", "content_objects.content_hash"],
    )
)
dispatches = scoped_table(
    "research_dispatches",
    Column("dispatch_id", Text, primary_key=True),
    Column("status", Text, nullable=False),
    Column("owner", Text),
    Column("lease_until", Float),
    Column("payload", JSONB, nullable=False),
)

# A delivery is a frozen, reviewable projection of an answer or report artifact.
# The body remains in ContentStore; decisions are append-only business records.
deliverables = scoped_table(
    "research_deliverables",
    Column("delivery_id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("origin_id", Text, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("manifest_hash", String(64), nullable=False),
    Column("status", Text, nullable=False),
    Column("profile", Text, nullable=False),
    Column("as_of", Text, nullable=False),
    Column("coverage", JSONB, nullable=False),
    Column("manifest", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
deliverables.append_constraint(UniqueConstraint("workspace_id", "session_id", "kind", "origin_id"))
deliverables.append_constraint(
    ForeignKeyConstraint(
        ["workspace_id", "content_hash"],
        ["content_objects.workspace_id", "content_objects.content_hash"],
    )
)
Index(
    "ix_deliverables_status",
    deliverables.c.workspace_id,
    deliverables.c.session_id,
    deliverables.c.status,
)
review_decisions = scoped_table(
    "research_review_decisions",
    Column("decision_id", Text, primary_key=True),
    Column("delivery_id", Text, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("actor_role", Text, nullable=False),
    Column("decision", Text, nullable=False),
    Column("note", Text, nullable=False),
    Column("manifest_hash", String(64), nullable=False),
    Column("created_at", Float, nullable=False),
)
review_decisions.append_column(
    Column("reviewed_claim_ids", JSONB, nullable=False, server_default="[]")
)
review_decisions.append_constraint(
    ForeignKeyConstraint(
        ["workspace_id", "session_id", "delivery_id"],
        [
            "research_deliverables.workspace_id",
            "research_deliverables.session_id",
            "research_deliverables.delivery_id",
        ],
        ondelete="CASCADE",
    )
)
collection_attempts = scoped_table(
    "research_collection_attempts",
    Column("attempt_id", Text, primary_key=True),
    Column("channel", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("query_hash", String(64), nullable=False),
    Column("outcome", Text, nullable=False),
    Column("errors", JSONB, nullable=False),
    Column("window_start", Text),
    Column("window_end", Text),
    Column("created_at", Float, nullable=False),
)
Index(
    "ix_collection_channel",
    collection_attempts.c.workspace_id,
    collection_attempts.c.session_id,
    collection_attempts.c.channel,
)

tool_runs = Table(
    "tool_runs",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("workspace_id", String(64), nullable=False),
    Column("session_id", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("predecessor", Text),
    Column("updated", Float, nullable=False),
)
tool_steps = Table(
    "tool_steps",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("step_id", Text, primary_key=True),
    Column("status", Text, nullable=False),
    Column("updated", Float, nullable=False),
    ForeignKeyConstraint(["run_id"], ["tool_runs.run_id"]),
)
tool_operations = Table(
    "tool_operations",
    metadata,
    Column("operation_id", Text, primary_key=True),
    Column("workspace_id", String(64), nullable=False),
    Column("run_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("step_id", Text, nullable=False),
    Column("call_id", Text, nullable=False),
    Column("tool", Text, nullable=False),
    Column("contract_version", Text, nullable=False),
    Column("input_digest", String(64), nullable=False),
    Column("effect", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("owner", Text),
    Column("host_id", Text),
    Column("pid", Integer),
    Column("lease_until", Float),
    Column("created", Float, nullable=False),
    Column("updated", Float, nullable=False),
    Column("error_code", Text),
    Column("result_ref", Text),
    Column("external_request_id", Text),
    Column("idempotency_key", Text),
    Column("reconciliation", Text),
    Column("resources", JSONB, nullable=False),
    UniqueConstraint("workspace_id", "session_id", "scope", "call_id"),
    ForeignKeyConstraint(["run_id", "step_id"], ["tool_steps.run_id", "tool_steps.step_id"]),
    CheckConstraint(
        "status IN ('prepared','running','succeeded','failed','partial',"
        "'uncertain','cancelled','blocked')"
    ),
)
Index(
    "ix_operations_recovery",
    tool_operations.c.workspace_id,
    tool_operations.c.session_id,
    tool_operations.c.scope,
    tool_operations.c.status,
)
tool_attempts = Table(
    "tool_attempts",
    metadata,
    Column("attempt_id", Text, primary_key=True),
    Column("operation_id", Text, nullable=False),
    Column("number", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("created", Float, nullable=False),
    Column("updated", Float, nullable=False),
    ForeignKeyConstraint(["operation_id"], ["tool_operations.operation_id"]),
    UniqueConstraint("operation_id", "number"),
)
api_attempts = Table(
    "api_attempts",
    metadata,
    Column("attempt_id", Text, primary_key=True),
    Column("workspace_id", String(64), nullable=False),
    Column("session_id", Text, nullable=False),
    Column("request_id", Text, nullable=False),
    Column("record", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("updated", Float, nullable=False),
)
legacy_import_records = Table(
    "legacy_import_records",
    metadata,
    Column("source_path", Text, primary_key=True),
    Column("source_hash", String(64), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("workspace_id", String(64), nullable=False),
    Column("session_id", Text),
    Column("report", JSONB, nullable=False),
    Column("imported_at", Float, nullable=False),
)


# Queryable, scoped relationships. Deferred constraints allow atomic graph revisions.
RELATIONS = (
    ("plans", "context_id", "research_contexts"),
    ("sources", "execution_id", "research_executions"),
    ("evidence_pool", "source_id", "research_sources"),
    ("artifacts", "execution_id", "research_executions"),
)
for field, column, target in RELATIONS:
    table = entities[field]
    table.append_column(Column(column, Text))
    table.append_constraint(
        ForeignKeyConstraint(
            ["workspace_id", "session_id", column],
            [f"{target}.workspace_id", f"{target}.session_id", f"{target}.record_id"],
            name=f"fk_{table.name}_{column}",
            deferrable=True,
            initially="DEFERRED",
        )
    )
Index(
    "ix_evidence_source",
    entities["evidence_pool"].c.workspace_id,
    entities["evidence_pool"].c.session_id,
    entities["evidence_pool"].c.source_id,
)
Index(
    "ix_plans_revision",
    entities["plans"].c.workspace_id,
    entities["plans"].c.session_id,
    entities["plans"].c.project_id,
    entities["plans"].c.revision,
)
Index(
    "ix_artifacts_task",
    entities["artifacts"].c.workspace_id,
    entities["artifacts"].c.session_id,
    entities["artifacts"].c.task_id,
    entities["artifacts"].c.status,
)
Index("ix_projects_status", entities["project"].c.workspace_id, entities["project"].c.status)
Index(
    "ix_research_workspace_updated",
    research_sessions.c.workspace_id,
    research_sessions.c.updated_at,
)
Index(
    "ix_operations_resources",
    tool_operations.c.workspace_id,
    tool_operations.c.scope,
    tool_operations.c.status,
)
Index(
    "ix_api_retention", api_attempts.c.workspace_id, api_attempts.c.status, api_attempts.c.updated
)
Index(
    "ix_attempts_operation",
    tool_attempts.c.operation_id,
    tool_attempts.c.status,
    tool_attempts.c.updated,
)
for table in (tool_runs, tool_operations, api_attempts):
    table.append_constraint(
        ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.workspace_id"], name=f"fk_{table.name}_workspace"
        )
    )
tool_runs.append_constraint(
    UniqueConstraint("run_id", "workspace_id", name="uq_tool_run_workspace")
)
tool_operations.append_constraint(
    ForeignKeyConstraint(
        ["run_id", "workspace_id"],
        ["tool_runs.run_id", "tool_runs.workspace_id"],
        name="fk_operation_run_workspace",
    )
)
legacy_import_records.append_constraint(
    UniqueConstraint("source_path", name="uq_legacy_source_path")
)

research_sessions.append_column(Column("parent_session_id", String(128)))
research_sessions.append_constraint(
    ForeignKeyConstraint(
        ["workspace_id", "parent_session_id"],
        ["research_sessions.workspace_id", "research_sessions.session_id"],
        name="fk_session_parent",
        ondelete="CASCADE",
    )
)
research_sessions.append_constraint(
    CheckConstraint("parent_session_id <> session_id", name="ck_session_not_own_parent")
)
Index("ix_session_parent", research_sessions.c.workspace_id, research_sessions.c.parent_session_id)


conversation_turns = scoped_table(
    "conversation_turns",
    Column("request_id", Text, primary_key=True),
    Column("command", JSONB, nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("digest_version", Integer, nullable=False),
    Column("message_id", Text, nullable=False),
    Column("effective_input", JSONB, nullable=False),
    Column("ack", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("sequence", Integer, nullable=False, server_default="0"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
conversation_turns.append_constraint(
    CheckConstraint("status IN ('accepted','running','completed','failed','interrupted')")
)
conversation_checkpoints = scoped_table(
    "conversation_checkpoints",
    Column("request_id", Text, primary_key=True),
    Column("sequence", Integer, primary_key=True),
    Column("phase", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("messages_ref", String(64), nullable=False),
    Column("operations", JSONB, nullable=False),
    Column("projection", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
)
conversation_checkpoints.append_constraint(
    ForeignKeyConstraint(
        ["workspace_id", "session_id", "request_id"],
        [
            "conversation_turns.workspace_id",
            "conversation_turns.session_id",
            "conversation_turns.request_id",
        ],
        ondelete="CASCADE",
    )
)
conversation_checkpoints.append_constraint(
    ForeignKeyConstraint(
        ["workspace_id", "messages_ref"],
        ["content_objects.workspace_id", "content_objects.content_hash"],
    )
)


def _workspace_asset(name: str, *columns: Column[Any]) -> Table:
    return Table(
        name,
        metadata,
        Column("workspace_id", String(64), primary_key=True),
        *columns,
        ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"]),
    )


research_specs = _workspace_asset(
    "research_specs",
    Column("spec_id", Text, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("project_id", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
)
research_observations = _workspace_asset(
    "research_observations",
    Column("observation_id", Text, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("content_hash", String(64), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
)
research_model_runs = _workspace_asset(
    "research_model_runs",
    Column("model_run_id", Text, primary_key=True),
    Column("spec_revision", Integer, nullable=False),
    Column("skill_id", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
)
research_quality_certificates = _workspace_asset(
    "research_quality_certificates",
    Column("artifact_version_id", Text, primary_key=True),
    Column("payload", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
)
research_runs = _workspace_asset(
    "research_runs",
    Column("run_id", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("project_id", Text, nullable=False),
    Column("request_id", Text, nullable=False),
    Column("epoch", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
Index(
    "ix_research_runs_session",
    research_runs.c.workspace_id,
    research_runs.c.session_id,
    research_runs.c.status,
)
Index(
    "ix_research_runs_project",
    research_runs.c.workspace_id,
    research_runs.c.project_id,
    research_runs.c.status,
)
research_run_prompts = _workspace_asset(
    "research_run_prompts",
    Column("prompt_id", Text, primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("epoch", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
)
research_run_events = _workspace_asset(
    "research_run_events",
    Column("session_id", Text, primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("epoch", Integer, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
)
project_asset_bindings = _workspace_asset(
    "project_asset_bindings",
    Column("project_id", Text, primary_key=True),
    Column("asset_id", Text, primary_key=True),
    Column("asset_kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
)
project_conversation_bindings = _workspace_asset(
    "project_conversation_bindings",
    Column("session_id", Text, primary_key=True),
    Column("project_id", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
Index(
    "ix_project_conversation_project",
    project_conversation_bindings.c.workspace_id,
    project_conversation_bindings.c.project_id,
)
asset_dependencies = _workspace_asset(
    "asset_dependencies",
    Column("upstream_id", Text, primary_key=True),
    Column("downstream_id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
)
Index(
    "ix_asset_dependencies_downstream",
    asset_dependencies.c.workspace_id,
    asset_dependencies.c.downstream_id,
)
