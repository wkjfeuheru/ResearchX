"""Frozen internal deliveries and append-only human review decisions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_business_delivery"
down_revision = "0003_child_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_deliverables",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("delivery_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("origin_id", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("coverage", JSONB(), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "session_id"],
            ["research_sessions.workspace_id", "research_sessions.session_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "content_hash"],
            ["content_objects.workspace_id", "content_objects.content_hash"],
        ),
        sa.UniqueConstraint("workspace_id", "session_id", "kind", "origin_id"),
    )
    op.create_index(
        "ix_deliverables_status", "research_deliverables", ["workspace_id", "session_id", "status"]
    )
    op.create_table(
        "research_review_decisions",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("decision_id", sa.Text(), primary_key=True),
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("actor_role", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "session_id", "delivery_id"],
            [
                "research_deliverables.workspace_id",
                "research_deliverables.session_id",
                "research_deliverables.delivery_id",
            ],
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; human review records must not be discarded")
