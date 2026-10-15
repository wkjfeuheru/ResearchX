"""Persist bounded collection outcomes without retaining sensitive query text."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_collection_attempts"
down_revision = "0004_business_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_collection_attempts",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("attempt_id", sa.Text(), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("errors", JSONB(), nullable=False),
        sa.Column("window_start", sa.Text()),
        sa.Column("window_end", sa.Text()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "session_id"],
            ["research_sessions.workspace_id", "research_sessions.session_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_collection_channel",
        "research_collection_attempts",
        ["workspace_id", "session_id", "channel"],
    )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; collection attempts are audit data")
