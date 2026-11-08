from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORLD_SCOPE_CHECK = (
    "(scope = 'sandbox' AND world_id IS NOT NULL) OR "
    "(scope IN ('demo', 'live') AND world_id IS NULL)"
)
_ORG_IDS = "SELECT id FROM organizations WHERE world_id IS NOT NULL"


def upgrade() -> None:
    op.create_table(
        "sandbox_worlds",
        sa.Column("id", sa.String(length=16), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "quality_seeded",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_index("ix_sandbox_worlds_expires_at", "sandbox_worlds", ["expires_at"])
    for table in (
        "organizations",
        "users",
        "quality_observations",
        "delay_observations",
    ):
        op.add_column(table, sa.Column("world_id", sa.String(length=16), nullable=True))
    op.execute(
        "INSERT INTO sandbox_worlds (id, created_at, expires_at, quality_seeded) "
        "SELECT 'legacy', now(), now(), false "
        "WHERE EXISTS (SELECT 1 FROM organizations WHERE sandbox AND id <> 'demo') "
        "OR EXISTS (SELECT 1 FROM quality_observations WHERE scope = 'sandbox') "
        "OR EXISTS (SELECT 1 FROM delay_observations WHERE scope = 'sandbox')"
    )
    op.execute(
        "UPDATE organizations SET world_id = 'legacy' WHERE sandbox AND id <> 'demo' "
        "AND EXISTS (SELECT 1 FROM sandbox_worlds WHERE id = 'legacy')"
    )
    op.execute(
        "UPDATE users u SET world_id = 'legacy' WHERE u.is_demo AND EXISTS ("
        "SELECT 1 FROM memberships m JOIN organizations o ON o.id = m.org_id "
        "WHERE m.user_id = u.id AND o.world_id = 'legacy')"
    )
    op.execute(
        "UPDATE quality_observations SET world_id = 'legacy' WHERE scope = 'sandbox' "
        "AND EXISTS (SELECT 1 FROM sandbox_worlds WHERE id = 'legacy')"
    )
    op.execute(
        "UPDATE delay_observations SET world_id = 'legacy' WHERE scope = 'sandbox' "
        "AND EXISTS (SELECT 1 FROM sandbox_worlds WHERE id = 'legacy')"
    )
    for table, ondelete in (
        ("organizations", "RESTRICT"),
        ("users", "RESTRICT"),
        ("quality_observations", "CASCADE"),
        ("delay_observations", "CASCADE"),
    ):
        op.create_foreign_key(
            f"{table}_world_id_fkey",
            table,
            "sandbox_worlds",
            ["world_id"],
            ["id"],
            ondelete=ondelete,
        )
        op.create_index(f"ix_{table}_world_id", table, ["world_id"])
    op.create_check_constraint(
        "quality_observations_world_check", "quality_observations", WORLD_SCOPE_CHECK
    )
    op.create_check_constraint(
        "delay_observations_world_check", "delay_observations", WORLD_SCOPE_CHECK
    )


def downgrade() -> None:
    op.execute("DELETE FROM quality_observations WHERE world_id IS NOT NULL")
    op.execute(f"DELETE FROM plan_sessions WHERE org_id IN ({_ORG_IDS})")
    op.execute(f"DELETE FROM coldchain_alerts WHERE org_id IN ({_ORG_IDS})")
    op.execute(f"DELETE FROM coldchain_containers WHERE org_id IN ({_ORG_IDS})")
    op.execute(f"DELETE FROM coldchain_bindings WHERE org_id IN ({_ORG_IDS})")
    op.execute(f"DELETE FROM coldchain_sim_runs WHERE org_id IN ({_ORG_IDS})")
    op.execute(f"DELETE FROM devices WHERE org_id IN ({_ORG_IDS})")
    op.execute(f"DELETE FROM lots WHERE shipper_org_id IN ({_ORG_IDS})")
    op.execute(f"DELETE FROM shipper_profiles WHERE org_id IN ({_ORG_IDS})")
    op.execute(f"DELETE FROM packhouses WHERE org_id IN ({_ORG_IDS})")
    op.execute("DELETE FROM delay_observations WHERE world_id IS NOT NULL")
    op.execute("DELETE FROM organizations WHERE world_id IS NOT NULL")
    op.execute("DELETE FROM users WHERE world_id IS NOT NULL")
    op.drop_constraint(
        "delay_observations_world_check", "delay_observations", type_="check"
    )
    op.drop_constraint(
        "quality_observations_world_check", "quality_observations", type_="check"
    )
    for table in (
        "delay_observations",
        "quality_observations",
        "users",
        "organizations",
    ):
        op.drop_index(f"ix_{table}_world_id", table_name=table)
        op.drop_constraint(f"{table}_world_id_fkey", table, type_="foreignkey")
        op.drop_column(table, "world_id")
    op.drop_index("ix_sandbox_worlds_expires_at", table_name="sandbox_worlds")
    op.drop_table("sandbox_worlds")
