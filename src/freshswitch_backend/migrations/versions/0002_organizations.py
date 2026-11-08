"""organizations, users and org scope"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEMO_ORG = "demo"
DEMO_ORG_NAME = "시연"

ORG_SCOPED = (
    ("plan_sessions", "org_id"),
    ("lots", "shipper_org_id"),
    ("coldchain_containers", "org_id"),
    ("coldchain_alerts", "org_id"),
    ("coldchain_bindings", "org_id"),
    ("coldchain_sim_runs", "org_id"),
)


def _now() -> sa.TextClause:
    return sa.text("now()")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
    ]


def upgrade() -> None:
    organizations = op.create_table(
        "organizations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "type IN ('platform', 'shipper', 'forwarder', 'demo')",
            name="organizations_type_check",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_organizations_type"), "organizations", ["type"], unique=False)
    op.bulk_insert(organizations, [{"id": DEMO_ORG, "type": "demo", "name": DEMO_ORG_NAME}])

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("is_demo", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "memberships",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=20), server_default="member", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'member')", name="memberships_role_check"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "org_id"),
    )
    op.create_index(op.f("ix_memberships_org_id"), "memberships", ["org_id"], unique=False)
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("active_org_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["active_org_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index(
        op.f("ix_auth_sessions_expires_at"), "auth_sessions", ["expires_at"], unique=False
    )
    op.create_index(op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"], unique=False)
    op.create_table(
        "packhouses",
        sa.Column("id", sa.String(length=100), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("region", sa.String(length=100), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=200), server_default="등록", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_packhouses_org_id"), "packhouses", ["org_id"], unique=False)
    op.create_table(
        "shipper_profiles",
        sa.Column("id", sa.String(length=100), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("packhouse_id", sa.String(length=100), nullable=False),
        sa.Column(
            "product_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=200), server_default="등록", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["packhouse_id"], ["packhouses.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_shipper_profiles_org_id"), "shipper_profiles", ["org_id"], unique=False
    )
    op.create_index(
        op.f("ix_shipper_profiles_packhouse_id"), "shipper_profiles", ["packhouse_id"], unique=False
    )
    op.create_table(
        "devices",
        sa.Column("device_id", sa.String(length=100), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("device_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_devices_org_id"), "devices", ["org_id"], unique=False)
    op.create_table(
        "plan_forwarders",
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("forwarder_org_id", sa.String(length=64), nullable=False),
        sa.Column("assigned_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["forwarder_org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["plan_sessions.plan_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("plan_id", "forwarder_org_id"),
    )
    op.create_index(
        op.f("ix_plan_forwarders_forwarder_org_id"),
        "plan_forwarders",
        ["forwarder_org_id"],
        unique=False,
    )

    for table, column in ORG_SCOPED:
        op.add_column(
            table,
            sa.Column(column, sa.String(length=64), server_default=DEMO_ORG, nullable=False),
        )
        op.create_index(op.f(f"ix_{table}_{column}"), table, [column], unique=False)
        op.create_foreign_key(
            f"{table}_{column}_fkey", table, "organizations", [column], ["id"], ondelete="RESTRICT"
        )


def downgrade() -> None:
    for table, column in reversed(ORG_SCOPED):
        op.drop_constraint(f"{table}_{column}_fkey", table, type_="foreignkey")
        op.drop_index(op.f(f"ix_{table}_{column}"), table_name=table)
        op.drop_column(table, column)

    op.drop_index(op.f("ix_plan_forwarders_forwarder_org_id"), table_name="plan_forwarders")
    op.drop_table("plan_forwarders")
    op.drop_index(op.f("ix_devices_org_id"), table_name="devices")
    op.drop_table("devices")
    op.drop_index(op.f("ix_shipper_profiles_packhouse_id"), table_name="shipper_profiles")
    op.drop_index(op.f("ix_shipper_profiles_org_id"), table_name="shipper_profiles")
    op.drop_table("shipper_profiles")
    op.drop_index(op.f("ix_packhouses_org_id"), table_name="packhouses")
    op.drop_table("packhouses")
    op.drop_index(op.f("ix_auth_sessions_user_id"), table_name="auth_sessions")
    op.drop_index(op.f("ix_auth_sessions_expires_at"), table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index(op.f("ix_memberships_org_id"), table_name="memberships")
    op.drop_table("memberships")
    op.drop_table("users")
    op.drop_index(op.f("ix_organizations_type"), table_name="organizations")
    op.drop_table("organizations")
