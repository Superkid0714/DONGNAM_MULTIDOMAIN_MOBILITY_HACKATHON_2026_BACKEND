from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("plan_change_requests_type_check", "plan_change_requests", type_="check")
    op.create_check_constraint(
        "plan_change_requests_type_check",
        "plan_change_requests",
        "type IN ('reduce', 'cancel', 'air_switch', 'increase')",
    )
    op.create_table(
        "quantity_policies",
        sa.Column(
            "plan_id",
            sa.String(100),
            sa.ForeignKey("plan_sessions.plan_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("voyage_id", sa.String(100), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_quantity_policies_org_id", "quantity_policies", ["org_id"])
    op.create_table(
        "quantity_ledger_events",
        sa.Column("event_id", sa.String(250), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(100),
            sa.ForeignKey("plan_sessions.plan_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("voyage_id", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_quantity_ledger_events_plan_id", "quantity_ledger_events", ["plan_id"])
    op.create_index("ix_quantity_ledger_events_voyage_id", "quantity_ledger_events", ["voyage_id"])


def downgrade() -> None:
    op.drop_constraint("plan_change_requests_type_check", "plan_change_requests", type_="check")
    op.create_check_constraint(
        "plan_change_requests_type_check",
        "plan_change_requests",
        "type IN ('reduce', 'cancel', 'air_switch')",
    )
    op.drop_table("quantity_ledger_events")
    op.drop_table("quantity_policies")
