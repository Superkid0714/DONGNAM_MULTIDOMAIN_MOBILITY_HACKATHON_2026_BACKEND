from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("sandbox", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute(
        "UPDATE organizations SET sandbox = true WHERE id IN ("
        "SELECT m.org_id FROM memberships m JOIN users u ON u.id = m.user_id "
        "WHERE u.is_demo)"
    )


def downgrade() -> None:
    op.drop_column("organizations", "sandbox")
