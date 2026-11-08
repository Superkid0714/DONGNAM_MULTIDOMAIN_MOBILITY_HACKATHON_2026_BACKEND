from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_execution_numbers",
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("container_id", sa.String(length=100), nullable=False),
        sa.Column("booking_no", sa.String(length=30), nullable=True),
        sa.Column("container_no", sa.String(length=11), nullable=True),
        sa.Column("seal_no", sa.String(length=20), nullable=True),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plan_sessions.plan_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("plan_id", "container_id"),
    )


def downgrade() -> None:
    op.drop_table("plan_execution_numbers")
