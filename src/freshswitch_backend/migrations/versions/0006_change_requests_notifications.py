from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_change_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("shipper_org_id", sa.String(length=64), nullable=False),
        sa.Column("shipper_id", sa.String(length=100), nullable=False),
        sa.Column("lot_id", sa.String(length=120), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("new_pallets", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="open", nullable=False),
        sa.Column("estimate", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("replan_id", sa.String(length=64), nullable=True),
        sa.Column("alternative_id", sa.String(length=40), nullable=True),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("decision_note", sa.String(length=500), nullable=True),
        sa.Column("requested_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "type IN ('reduce', 'cancel', 'air_switch')", name="plan_change_requests_type_check"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'applied', 'rejected')", name="plan_change_requests_status_check"
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["plan_sessions.plan_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shipper_org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seq"),
    )
    op.create_index(
        "ix_plan_change_requests_plan_id", "plan_change_requests", ["plan_id"], unique=False
    )
    op.create_index(
        "ix_plan_change_requests_shipper_org_id",
        "plan_change_requests",
        ["shipper_org_id"],
        unique=False,
    )
    op.create_index(
        "ix_plan_change_requests_org_status",
        "plan_change_requests",
        ["org_id", "status"],
        unique=False,
    )
    op.create_index(
        "ux_plan_change_requests_open",
        "plan_change_requests",
        ["plan_id", "lot_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("shipper_id", sa.String(length=100), nullable=True),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("change_request_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("channel", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.String(length=2000), nullable=False),
        sa.Column("link", sa.String(length=500), nullable=False),
        sa.Column("dedupe_key", sa.String(length=300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["plan_sessions.plan_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["change_request_id"], ["plan_change_requests.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seq"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_notifications_plan_id", "notifications", ["plan_id"], unique=False)
    op.create_index(
        "ix_notifications_change_request_id", "notifications", ["change_request_id"], unique=False
    )
    op.create_index("ix_notifications_org_seq", "notifications", ["org_id", "seq"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_notifications_org_seq", table_name="notifications")
    op.drop_index("ix_notifications_change_request_id", table_name="notifications")
    op.drop_index("ix_notifications_plan_id", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ux_plan_change_requests_open", table_name="plan_change_requests")
    op.drop_index("ix_plan_change_requests_org_status", table_name="plan_change_requests")
    op.drop_index("ix_plan_change_requests_shipper_org_id", table_name="plan_change_requests")
    op.drop_index("ix_plan_change_requests_plan_id", table_name="plan_change_requests")
    op.drop_table("plan_change_requests")
