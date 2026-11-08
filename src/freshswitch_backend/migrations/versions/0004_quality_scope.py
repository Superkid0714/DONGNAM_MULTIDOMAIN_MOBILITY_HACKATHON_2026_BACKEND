from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "quality_observations",
        sa.Column("scope", sa.String(length=20), server_default="demo", nullable=False),
    )
    op.create_check_constraint(
        "quality_observations_scope_check",
        "quality_observations",
        "scope IN ('demo', 'sandbox', 'live')",
    )
    op.create_index("ix_quality_observations_scope_seq", "quality_observations", ["scope", "seq"])
    op.execute(
        "UPDATE quality_observations q SET scope = CASE WHEN o.sandbox THEN 'sandbox' "
        "ELSE 'live' END FROM plan_sessions p JOIN organizations o ON o.id = p.org_id "
        "WHERE q.plan_id = p.plan_id AND p.org_id <> 'demo'"
    )


def downgrade() -> None:
    op.execute("DELETE FROM quality_observations WHERE scope <> 'demo'")
    op.execute(
        "DELETE FROM app_meta WHERE key IN ('quality_seeded:sandbox', 'quality_seeded:live')"
    )
    op.drop_index("ix_quality_observations_scope_seq", table_name="quality_observations")
    op.drop_constraint("quality_observations_scope_check", "quality_observations", type_="check")
    op.drop_column("quality_observations", "scope")
