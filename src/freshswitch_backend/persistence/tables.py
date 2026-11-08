from datetime import date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Sequence,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

Json = dict[str, Any]


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        Json: JSONB,
        list[Any]: JSONB,
        datetime: DateTime(timezone=True),
        date: Date,
    }


ALERT_SEQ = Sequence("coldchain_alert_seq", metadata=Base.metadata)


class MetaRow(Base):
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Json]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class LotRow(Base):
    __tablename__ = "lots"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(100), index=True)
    shipper_id: Mapped[str] = mapped_column(String(100), index=True)
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
    scenario_id: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[int]
    status: Mapped[str] = mapped_column(String(40))
    lot_count: Mapped[int]
    totals: Mapped[Json]
    created_at: Mapped[datetime] = mapped_column(index=True)
    updated_at: Mapped[datetime]
    payload: Mapped[Json]
    committed: Mapped[Json]


def _plan_fk() -> ForeignKey:
    return ForeignKey("plan_sessions.plan_id", ondelete="CASCADE")


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
    record_key: Mapped[str] = mapped_column(String(180), _container_fk(), index=True)
    container_id: Mapped[str] = mapped_column(String(100), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), index=True)
    key: Mapped[str]
    created_at: Mapped[datetime]
    payload: Mapped[Json]


class BindingRow(Base):
    __tablename__ = "coldchain_bindings"

    device_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    container_id: Mapped[str] = mapped_column(String(100), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    payload: Mapped[Json]


class RunRow(Base):
    __tablename__ = "coldchain_sim_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    container_id: Mapped[str] = mapped_column(String(100), index=True)
    done: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    payload: Mapped[Json]


class ObservationRow(Base):
    __tablename__ = "quality_observations"
    __table_args__ = (Index("ix_quality_observations_plan_lot", "plan_id", "lot_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    origin: Mapped[str] = mapped_column(String(20), index=True)
    product_id: Mapped[str] = mapped_column(String(100), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64))
    lot_id: Mapped[str | None] = mapped_column(String(120))
    recorded_at: Mapped[datetime]
    inspection_revision: Mapped[int | None]
    payload: Mapped[Json]
