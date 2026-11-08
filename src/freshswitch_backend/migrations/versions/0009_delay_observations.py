from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delay_observations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("org_id", sa.String(length=64), server_default="demo", nullable=False),
        sa.Column("scope", sa.String(length=20), server_default="demo", nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("voyage_id", sa.String(length=64), nullable=False),
        sa.Column("vessel_key", sa.String(length=200), nullable=False),
        sa.Column("vessel_name", sa.String(length=200), nullable=False),
        sa.Column("call_sign", sa.String(length=32), nullable=True),
        sa.Column("matched_by", sa.String(length=20), nullable=False),
        sa.Column("terminal", sa.String(length=100), nullable=True),
        sa.Column("vessel_kind", sa.String(length=100), nullable=True),
        sa.Column("planned_departure_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("departure_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delay_hours", sa.Float(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope IN ('demo', 'sandbox', 'live')", name="delay_observations_scope_check"
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "vessel_key",
            "planned_departure_at",
            name="uq_delay_observations_org_vessel_planned",
        ),
    )
    op.create_index(
        op.f("ix_delay_observations_org_id"), "delay_observations", ["org_id"], unique=False
    )
    op.create_index("ix_delay_observations_scope_id", "delay_observations", ["scope", "id"])


def downgrade() -> None:
    op.drop_index("ix_delay_observations_scope_id", table_name="delay_observations")
    op.drop_index(op.f("ix_delay_observations_org_id"), table_name="delay_observations")
    op.drop_table("delay_observations")
