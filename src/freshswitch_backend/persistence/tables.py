from datetime import date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Sequence,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

Json = dict[str, Any]

DEMO_ORG = "demo"
DEMO_SCOPE = "demo"
LEARNING_SCOPES = ("demo", "sandbox", "live")
ORG_TYPES = ("platform", "shipper", "forwarder", "demo")
MEMBERSHIP_ROLES = ("admin", "member")
WORLD_SCOPE_CHECK = (
    "(scope = 'sandbox' AND world_id IS NOT NULL) OR "
    "(scope IN ('demo', 'live') AND world_id IS NULL)"
)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        Json: JSONB,
        list[Any]: JSONB,
        datetime: DateTime(timezone=True),
        date: Date,
    }


ALERT_SEQ = Sequence("coldchain_alert_seq", metadata=Base.metadata)


def _org_fk(ondelete: str = "RESTRICT") -> ForeignKey:
    return ForeignKey("organizations.id", ondelete=ondelete)


def _org_column() -> Mapped[str]:
    return mapped_column(String(64), _org_fk(), index=True, server_default=DEMO_ORG)


def _user_fk(ondelete: str = "CASCADE") -> ForeignKey:
    return ForeignKey("users.id", ondelete=ondelete)


class MetaRow(Base):
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Json]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class QuantityPolicyRow(Base):
    __tablename__ = "quantity_policies"

    plan_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("plan_sessions.plan_id", ondelete="CASCADE"), primary_key=True
    )
    voyage_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), _org_fk(), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[Json]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class QuantityLedgerEventRow(Base):
    __tablename__ = "quantity_ledger_events"

    event_id: Mapped[str] = mapped_column(String(250), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("plan_sessions.plan_id", ondelete="CASCADE"), index=True
    )
    voyage_id: Mapped[str] = mapped_column(String(100), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    at: Mapped[datetime]
    payload: Mapped[Json]


class SandboxWorldRow(Base):
    __tablename__ = "sandbox_worlds"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime] = mapped_column(index=True)
    reset_at: Mapped[datetime | None]
    quality_seeded: Mapped[bool] = mapped_column(server_default=text("false"))


class OrganizationRow(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(
            "type IN ('platform', 'shipper', 'forwarder', 'demo')",
            name="organizations_type_check",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(200))
    sandbox: Mapped[bool] = mapped_column(server_default=text("false"))
    world_id: Mapped[str | None] = mapped_column(
        String(16), ForeignKey("sandbox_worlds.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(200))
    is_demo: Mapped[bool] = mapped_column(server_default=text("false"))
    world_id: Mapped[str | None] = mapped_column(
        String(16), ForeignKey("sandbox_worlds.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class MembershipRow(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member')", name="memberships_role_check"),
    )

    user_id: Mapped[str] = mapped_column(String(64), _user_fk(), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(64), _org_fk("CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(20), server_default="member")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AuthSessionRow(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), _user_fk(), index=True)
    active_org_id: Mapped[str | None] = mapped_column(String(64), _org_fk("SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(index=True)
    last_seen_at: Mapped[datetime | None]


class OrgInvitationRow(Base):
    __tablename__ = "org_invitations"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member')", name="org_invitations_role_check"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(20), server_default="member")
    created_by: Mapped[str | None] = mapped_column(String(64), _user_fk("SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    used_by: Mapped[str | None] = mapped_column(String(64), _user_fk("SET NULL"))
    used_at: Mapped[datetime | None]


class PackhouseRow(Base):
    __tablename__ = "packhouses"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    region: Mapped[str] = mapped_column(String(100))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(200), server_default="등록")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ShipperProfileRow(Base):
    __tablename__ = "shipper_profiles"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    packhouse_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("packhouses.id", ondelete="RESTRICT"), index=True
    )
    product_ids: Mapped[list[Any]] = mapped_column(server_default=text("'[]'::jsonb"))
    source: Mapped[str] = mapped_column(String(200), server_default="등록")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ProductRow(Base):
    __tablename__ = "shipper_products"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"), index=True)
    data: Mapped[Json]
    replaces_id: Mapped[str | None] = mapped_column(String(100))
    retired: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProductArchiveRow(Base):
    __tablename__ = "shipper_product_archives"

    org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    archived_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DeviceRow(Base):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime | None]


class LotRow(Base):
    __tablename__ = "lots"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(100), index=True)
    shipper_id: Mapped[str] = mapped_column(String(100), index=True)
    shipper_org_id: Mapped[str] = _org_column()
    product_id: Mapped[str] = mapped_column(String(100), index=True)
    pallets: Mapped[int] = mapped_column(Integer)
    ready_date: Mapped[date] = mapped_column(index=True)
    destination: Mapped[str] = mapped_column(String(100), index=True)
    sell_by_date: Mapped[date]
    sell_window: Mapped[str] = mapped_column(String(20), default="normal")
    packhouse: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(index=True)
    updated_at: Mapped[datetime]


class PlanRow(Base):
    __tablename__ = "plan_sessions"

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    org_id: Mapped[str] = _org_column()
    scenario_id: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[int]
    status: Mapped[str] = mapped_column(String(40))
    lot_count: Mapped[int]
    totals: Mapped[Json]
    created_at: Mapped[datetime] = mapped_column(index=True)
    updated_at: Mapped[datetime]
    payload: Mapped[Json]
    committed: Mapped[Json]
    confirmation_round: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    needs_replan: Mapped[bool] = mapped_column(server_default=text("false"))
    replan_candidates: Mapped[list[Any]] = mapped_column(server_default=text("'[]'::jsonb"))


def _plan_fk() -> ForeignKey:
    return ForeignKey("plan_sessions.plan_id", ondelete="CASCADE")


class ForwarderResourceRow(Base):
    __tablename__ = "forwarder_resources"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"), index=True)
    data: Mapped[Json]
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    allocated_plan_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("plan_sessions.plan_id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class PlanConfirmationRow(Base):
    __tablename__ = "plan_shipper_confirmations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name="plan_shipper_confirmations_status_check",
        ),
        Index("ix_plan_shipper_confirmations_org_status", "shipper_org_id", "status"),
    )

    plan_id: Mapped[str] = mapped_column(String(64), _plan_fk(), primary_key=True)
    round: Mapped[int] = mapped_column(Integer, primary_key=True)
    shipper_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    shipper_org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"))
    lot_ids: Mapped[list[Any]]
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    reason: Mapped[str | None] = mapped_column(String(500))
    auto_accepted: Mapped[bool] = mapped_column(server_default=text("false"))
    decided_by: Mapped[str | None] = mapped_column(String(64), _user_fk("SET NULL"))
    created_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]


class PlanForwarderRow(Base):
    __tablename__ = "plan_forwarders"

    plan_id: Mapped[str] = mapped_column(String(64), _plan_fk(), primary_key=True)
    forwarder_org_id: Mapped[str] = mapped_column(
        String(64), _org_fk("CASCADE"), primary_key=True, index=True
    )
    assigned_by: Mapped[str | None] = mapped_column(String(64), _user_fk("SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ExecutionNumbersRow(Base):
    __tablename__ = "plan_execution_numbers"

    plan_id: Mapped[str] = mapped_column(String(64), _plan_fk(), primary_key=True)
    container_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    booking_no: Mapped[str | None] = mapped_column(String(30))
    container_no: Mapped[str | None] = mapped_column(String(11))
    seal_no: Mapped[str | None] = mapped_column(String(20))
    updated_by: Mapped[str | None] = mapped_column(String(64), _user_fk("SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime]


class PendingRow(Base):
    __tablename__ = "plan_pending_replans"

    replan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    plan_id: Mapped[str] = mapped_column(String(64), _plan_fk(), index=True)
    base_version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    payload: Mapped[Json]


class OrderRow(Base):
    __tablename__ = "plan_orders"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    plan_id: Mapped[str] = mapped_column(String(64), _plan_fk(), index=True)
    status: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    payload: Mapped[Json]


class IdempotencyRow(Base):
    __tablename__ = "plan_idempotency_keys"

    plan_id: Mapped[str] = mapped_column(String(64), _plan_fk(), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    fingerprint: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    payload: Mapped[Json]


class ContainerRow(Base):
    __tablename__ = "coldchain_containers"

    record_key: Mapped[str] = mapped_column(String(180), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    org_id: Mapped[str] = _org_column()
    container_id: Mapped[str] = mapped_column(String(100), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(20))
    carry: Mapped[Json | None]
    history: Mapped[list[Any]]
    anchors: Mapped[list[Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


def _container_fk() -> ForeignKey:
    return ForeignKey("coldchain_containers.record_key", ondelete="CASCADE")


class ReadingRow(Base):
    __tablename__ = "coldchain_readings"
    __table_args__ = (Index("ix_coldchain_readings_record_ts", "record_key", "ts"),)

    record_key: Mapped[str] = mapped_column(String(180), _container_fk(), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    payload: Mapped[Json]


class AlertRow(Base):
    __tablename__ = "coldchain_alerts"
    __table_args__ = (UniqueConstraint("record_key", "key"),)

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    org_id: Mapped[str] = _org_column()
    record_key: Mapped[str] = mapped_column(String(180), _container_fk(), index=True)
    container_id: Mapped[str] = mapped_column(String(100), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), index=True)
    key: Mapped[str]
    created_at: Mapped[datetime]
    payload: Mapped[Json]


class BindingRow(Base):
    __tablename__ = "coldchain_bindings"

    device_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    org_id: Mapped[str] = _org_column()
    container_id: Mapped[str] = mapped_column(String(100), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    payload: Mapped[Json]


class RunRow(Base):
    __tablename__ = "coldchain_sim_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    org_id: Mapped[str] = _org_column()
    container_id: Mapped[str] = mapped_column(String(100), index=True)
    done: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    payload: Mapped[Json]


class ObservationRow(Base):
    __tablename__ = "quality_observations"
    __table_args__ = (
        Index("ix_quality_observations_plan_lot", "plan_id", "lot_id"),
        Index("ix_quality_observations_scope_seq", "scope", "seq"),
        CheckConstraint(
            "scope IN ('demo', 'sandbox', 'live')", name="quality_observations_scope_check"
        ),
        CheckConstraint(WORLD_SCOPE_CHECK, name="quality_observations_world_check"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    scope: Mapped[str] = mapped_column(String(20), server_default=DEMO_SCOPE)
    world_id: Mapped[str | None] = mapped_column(
        String(16), ForeignKey("sandbox_worlds.id", ondelete="CASCADE"), index=True
    )
    origin: Mapped[str] = mapped_column(String(20), index=True)
    product_id: Mapped[str] = mapped_column(String(100), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64))
    lot_id: Mapped[str | None] = mapped_column(String(120))
    recorded_at: Mapped[datetime]
    inspection_revision: Mapped[int | None]
    payload: Mapped[Json]


CHANGE_REQUEST_TYPES = ("reduce", "cancel", "air_switch")
CHANGE_REQUEST_STATUSES = ("open", "applied", "rejected")
OPEN_CHANGE_REQUEST_INDEX = "ux_plan_change_requests_open"


class ChangeRequestRow(Base):
    __tablename__ = "plan_change_requests"
    __table_args__ = (
        CheckConstraint(
            "type IN ('reduce', 'cancel', 'air_switch', 'increase')",
            name="plan_change_requests_type_check",
        ),
        CheckConstraint(
            "status IN ('open', 'applied', 'rejected')", name="plan_change_requests_status_check"
        ),
        Index("ix_plan_change_requests_org_status", "org_id", "status"),
        Index(
            OPEN_CHANGE_REQUEST_INDEX,
            "plan_id",
            "lot_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    plan_id: Mapped[str] = mapped_column(String(64), _plan_fk(), index=True)
    org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"))
    shipper_org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"), index=True)
    shipper_id: Mapped[str] = mapped_column(String(100))
    lot_id: Mapped[str] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(20))
    new_pallets: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), server_default="open")
    estimate: Mapped[Json | None]
    replan_id: Mapped[str | None] = mapped_column(String(64))
    alternative_id: Mapped[str | None] = mapped_column(String(40))
    order_id: Mapped[str | None] = mapped_column(String(64))
    decision_note: Mapped[str | None] = mapped_column(String(500))
    requested_by: Mapped[str | None] = mapped_column(String(64), _user_fk("SET NULL"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]


class NotificationRow(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_org_seq", "org_id", "seq"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"))
    shipper_id: Mapped[str | None] = mapped_column(String(100))
    plan_id: Mapped[str | None] = mapped_column(String(64), _plan_fk(), index=True)
    order_id: Mapped[str | None] = mapped_column(String(64))
    change_request_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("plan_change_requests.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(2000))
    link: Mapped[str] = mapped_column(String(500))
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True)
    created_at: Mapped[datetime]


class DelayObservationRow(Base):
    __tablename__ = "delay_observations"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "vessel_key",
            "planned_departure_at",
            name="uq_delay_observations_org_vessel_planned",
        ),
        Index("ix_delay_observations_scope_id", "scope", "id"),
        CheckConstraint(
            "scope IN ('demo', 'sandbox', 'live')", name="delay_observations_scope_check"
        ),
        CheckConstraint(WORLD_SCOPE_CHECK, name="delay_observations_world_check"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(64), _org_fk("CASCADE"), index=True, server_default=DEMO_ORG
    )
    scope: Mapped[str] = mapped_column(String(20), server_default=DEMO_SCOPE)
    world_id: Mapped[str | None] = mapped_column(
        String(16), ForeignKey("sandbox_worlds.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[str | None] = mapped_column(String(64))
    voyage_id: Mapped[str] = mapped_column(String(64))
    vessel_key: Mapped[str] = mapped_column(String(200))
    vessel_name: Mapped[str] = mapped_column(String(200))
    call_sign: Mapped[str | None] = mapped_column(String(32))
    matched_by: Mapped[str] = mapped_column(String(20))
    terminal: Mapped[str | None] = mapped_column(String(100))
    vessel_kind: Mapped[str | None] = mapped_column(String(100))
    planned_departure_at: Mapped[datetime]
    departure_at: Mapped[datetime]
    delay_hours: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
