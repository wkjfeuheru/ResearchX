"""Durable accepted commands and raw execution checkpoints."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_turn_journal"
down_revision = "0005_collection_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_turns",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("request_id", sa.Text(), primary_key=True),
        sa.Column("command", JSONB(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("digest_version", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("effective_input", JSONB(), nullable=False),
        sa.Column("ack", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "session_id"],
            ["research_sessions.workspace_id", "research_sessions.session_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("status IN ('accepted','running','completed','failed','interrupted')"),
    )
    op.create_table(
        "conversation_checkpoints",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("request_id", sa.Text(), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("messages_ref", sa.String(64), nullable=False),
        sa.Column("operations", JSONB(), nullable=False),
        sa.Column("projection", JSONB(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "session_id"],
            ["research_sessions.workspace_id", "research_sessions.session_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "session_id", "request_id"],
            [
                "conversation_turns.workspace_id",
                "conversation_turns.session_id",
                "conversation_turns.request_id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "messages_ref"],
            ["content_objects.workspace_id", "content_objects.content_hash"],
        ),
    )


def downgrade() -> None:
    raise RuntimeError("Accepted commands are recovery evidence; restore a verified backup")
