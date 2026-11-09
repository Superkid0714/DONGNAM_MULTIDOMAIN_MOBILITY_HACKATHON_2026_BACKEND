"""Immutable organization-owned quote evidence records."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operational_quote_records",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_operational_quote_records_org_id", "operational_quote_records", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_operational_quote_records_org_id", table_name="operational_quote_records")
    op.drop_table("operational_quote_records")
