from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forwarder_resources",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "allocated_plan_id",
            sa.String(64),
            sa.ForeignKey("plan_sessions.plan_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_forwarder_resources_org_id", "forwarder_resources", ["org_id"])
    op.create_index(
        "ix_forwarder_resources_allocated_plan_id", "forwarder_resources", ["allocated_plan_id"]
    )


def downgrade() -> None:
    op.drop_table("forwarder_resources")
