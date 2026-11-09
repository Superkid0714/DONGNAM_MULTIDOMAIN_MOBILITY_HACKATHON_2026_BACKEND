"""Add isolated robust quote observations; never alter existing tables."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "robust_quote_observations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("item", sa.String(100), nullable=False),
        sa.Column("origin", sa.String(200), nullable=False),
        sa.Column("destination", sa.String(200), nullable=False),
        sa.Column("equipment", sa.String(100), nullable=False),
        sa.Column("quoted_on", sa.Date(), nullable=False),
        sa.Column("minimum", sa.Numeric(18, 4), nullable=False),
        sa.Column("maximum", sa.Numeric(18, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("basis", sa.String(30), nullable=False),
        sa.Column("reference_pallets", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(2000), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_robust_quote_observations_org_id", "robust_quote_observations", ["org_id"])


def downgrade():
    op.drop_index("ix_robust_quote_observations_org_id", table_name="robust_quote_observations")
    op.drop_table("robust_quote_observations")
