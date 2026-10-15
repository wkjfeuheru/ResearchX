"""Workspace-level research assets, runs and quality records.

Idempotent: revision 0001 creates every table currently declared in schema.py,
so an existing database and a fresh install both reach the same catalog.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_research_assets"
down_revision = "0006_turn_journal"
branch_labels = None
depends_on = None

_TABLES = (
    "research_specs",
    "research_observations",
    "research_model_runs",
    "research_quality_certificates",
    "research_runs",
    "research_run_prompts",
    "research_run_events",
    "project_asset_bindings",
    "project_conversation_bindings",
    "asset_dependencies",
)


def upgrade() -> None:
    from researchx.storage.schema import metadata

    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    pending = [table for name, table in metadata.tables.items() if name in _TABLES and name not in existing]
    if pending:
        metadata.create_all(bind, tables=pending, checkfirst=True)
    columns = {column["name"] for column in sa.inspect(bind).get_columns("research_review_decisions")}
    if "reviewed_claim_ids" not in columns:
        op.add_column(
            "research_review_decisions",
            sa.Column("reviewed_claim_ids", JSONB, nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    raise RuntimeError("研究资产不提供削弱约束的反向迁移；请恢复已验证的备份")
