from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_sessions",
        sa.Column("confirmation_round", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "plan_sessions",
        sa.Column("needs_replan", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "plan_sessions",
        sa.Column(
            "replan_candidates",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_table(
        "plan_shipper_confirmations",
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("shipper_id", sa.String(length=100), nullable=False),
        sa.Column("shipper_org_id", sa.String(length=64), nullable=False),
        sa.Column("lot_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("auto_accepted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name="plan_shipper_confirmations_status_check",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["plan_sessions.plan_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shipper_org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("plan_id", "round", "shipper_id"),
    )
    op.create_index(
        "ix_plan_shipper_confirmations_org_status",
        "plan_shipper_confirmations",
        ["shipper_org_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE plan_sessions SET status = 'committed' "
            "WHERE status = 'pending_shipper_confirmation'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE plan_sessions "
            "SET payload = jsonb_set(payload, '{status}', '\"committed\"'::jsonb) "
            "WHERE payload->>'status' = 'pending_shipper_confirmation'"
        )
    )
    op.drop_index(
        "ix_plan_shipper_confirmations_org_status", table_name="plan_shipper_confirmations"
    )
    op.drop_table("plan_shipper_confirmations")
    op.drop_column("plan_sessions", "replan_candidates")
    op.drop_column("plan_sessions", "needs_replan")
    op.drop_column("plan_sessions", "confirmation_round")
