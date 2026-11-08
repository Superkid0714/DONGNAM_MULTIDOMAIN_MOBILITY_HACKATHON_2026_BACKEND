import copy
import enum
import re
import typing
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from functools import cache
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from freshswitch_backend.coldchain.models import Alert, ColdchainAction, LossSummary, TwinState
from freshswitch_backend.coldchain.power_plan import power_plan
from freshswitch_backend.domain.models import (
    Assignment,
    Scenario,
    SettlementBaselineKind,
    SettlementBillingRule,
)
from freshswitch_backend.execution.card import (
    EnteredNumbers,
    GensetInstruction,
    build_execution_card,
    mock_container_no,
    mock_seal_no,
)
from freshswitch_backend.execution.models import (
    ExecutionOrder,
    OrderLogEntry,
    ShipperConfirmation,
)
from freshswitch_backend.execution.orders import genset_instructions, short_name
from freshswitch_backend.execution.quality import QualitySettlement, adjusted_settlements
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import LotRow, OrganizationRow, PlanForwarderRow
from freshswitch_backend.planning.store import PlanConfirmationState, PlanSession
from freshswitch_backend.replan.models import (
    AddLotEvent,
    CancellationCharge,
    CancellationTransfer,
    FixedLot,
    RouteChange,
)

MONEY_TOTALS = (
    "total_baseline_cost",
    "total_shared_cost",
    "total_savings",
    "total_fee",
    "total_operational_baseline_cost",
    "total_operational_cost",
    "total_operational_savings",
)
MONEY_LISTS = ("settlements", "container_settlements", "settlement_tariffs", "shipper_costs")
HISTORY_MONEY = ("cost_before", "cost_after", "cost_delta")
CONFIRMATION_MONEY = (
    "cost_before",
    "cost_after",
    "cost_delta",
    "cancellation_adjustment",
    "net_cost_delta",
)
CANCELLATION_MONEY = (
    "container_cost",
    "share_per_pallet_before",
    "share_per_pallet_after",
    "fee",
    "consolidator_amount",
)
INSPECTION_FIELDS = (
    "lot_id",
    "shipper_id",
    "planned_loss_rate",
    "inspected_loss_rate",
    "inspected_at",
)
SETTLEMENT_ACTION = "settlement_recompute"
SETTLEMENT_TITLE = "정산 재계산"
ROUTE_FIELDS = frozenset(
    {
        "truck_id",
        "trip",
        "cargo_group",
        "capacity_pallets",
        "load_pallets",
        "stops",
        "earliest_departure",
        "latest_departure",
        "port_eta",
        "slack_minutes",
        "gate_status",
        "gate_window",
        "gate_delay_minutes",
        "gate_slot_start",
        "gate_slot_end",
    }
)
STOP_FIELDS = frozenset(
    {
        "stop_id",
        "packhouse_id",
        "lot_ids",
        "sequence",
        "pallets",
        "load_after",
        "ready_at",
        "eta",
        "latest_arrival",
        "cargo_group",
    }
)
PICKUP_FIELDS = frozenset(
    {
        "status",
        "deadline",
        "routes",
        "dropped_stop_ids",
        "solve_ms",
        "assumptions",
        "gate_mode",
        "gate_windows",
    }
)
ROUTE_CHANGE_MONEY = ("km_before", "km_after", "cost_before", "cost_after")
ROUTE_CHANGE_PACKHOUSES = (
    "packhouses_before",
    "packhouses_after",
    "packhouses_added",
    "packhouses_removed",
)
ROUTE_CHANGE_LOTS = ("lot_ids_before", "lot_ids_after")
UNKNOWN_OWNER_LABEL = "x"
TOTAL_COST_CLAUSE = re.compile(
    r"이며 총 비용은 (?:변동 없습니다|\S+(?: 원)? (?:늘어납니다|줄어듭니다))\."
)


class ForwarderShipperConfirmation(ShipperConfirmation):
    cost_before: int | None = None
    cost_after: int | None = None
    cost_delta: int | None = None
    cancellation_adjustment: int | None = None
    net_cost_delta: int | None = None


class ForwarderCancellationTransfer(CancellationTransfer):
    share_increase: int | None = None
    amount: int | None = None


class ForwarderCancellation(CancellationCharge):
    container_cost: int | None = None
    share_per_pallet_before: int | None = None
    share_per_pallet_after: int | None = None
    fee: int | None = None
    consolidator_amount: int | None = None
    transfers: list[ForwarderCancellationTransfer]


class ForwarderOrder(ExecutionOrder):
    shipper_confirmations: list[ForwarderShipperConfirmation]
    cancellation: ForwarderCancellation | None = None
    cancellation_charges: list[ForwarderCancellation] = Field(default_factory=list)


class ShipperRouteChange(RouteChange):
    km_before: float | None = None
    km_after: float | None = None
    cost_before: int | None = None
    cost_after: int | None = None


class ForwarderRef(BaseModel):
    org_id: str
    org_name: str


class PlanListItem(BaseModel):
    plan_id: str
    created_at: datetime
    status: str
    vessel: str | None
    departure: datetime | None
    shipper_count: int
    total_pallets: int
    forwarder: ForwarderRef | None = None


class ShipperPlanItem(BaseModel):
    plan_id: str
    created_at: datetime
    status: str
    vessel: str | None
    departure: datetime | None
    my_lot_ids: list[str]
    my_pallets: int
    total_pallets: int
    replan_pending: bool = False


class ShipperContainerNumbers(BaseModel):
    container_id: str
    voyage_id: str
    container_no: str
    seal_no: str
    mock: bool
    container_no_mock: bool
    seal_no_mock: bool


class ShipperSettlementRow(QualitySettlement):
    plan_id: str
    version: int
    vessel: str | None
    departure: datetime | None


class PlatformShipperShare(BaseModel):
    shipper_id: str
    shipper_org_name: str | None
    saving_krw: int
    fee_krw: int
    billing_rule: SettlementBillingRule = "performance"
    performance_fee_krw: int = Field(
        default=0,
        description="환급 전 금액. fee_before_refund_krw = performance_fee_krw + usage_fee_krw(시범 항차 제외)",
    )
    usage_fee_krw: int = Field(
        default=0,
        description="환급 전 금액. fee_before_refund_krw = performance_fee_krw + usage_fee_krw(시범 항차 제외)",
    )
    fee_before_refund_krw: int = 0
    quality_refund_krw: int = 0
    baseline_kind: SettlementBaselineKind = "air_solo"
    baseline_label: str = ""
    savings_baseline_label: str = ""
    pilot: bool = False


class PlatformSettlement(BaseModel):
    plan_id: str
    vessel: str | None
    departure: datetime | None
    total_saving_krw: int
    platform_fee_krw: int
    shippers: list[PlatformShipperShare]
    performance_fee_krw: int = Field(
        default=0,
        description="환급 전 금액. fee_before_refund_krw = performance_fee_krw + usage_fee_krw(시범 항차 제외)",
    )
    usage_fee_krw: int = Field(
        default=0,
        description="환급 전 금액. fee_before_refund_krw = performance_fee_krw + usage_fee_krw(시범 항차 제외)",
    )
    fee_before_refund_krw: int = 0
    quality_refund_krw: int = 0
    pilot: bool = False


OTHER_SHIPPER_LABEL = "다른 화주"
STOP_PREFIX = "stop-"
MIN_TEXT_LOT_LENGTH = 3
MIN_NUMERIC_TEXT_LOT_LENGTH = 6
MODEL_PACKAGE = "freshswitch_backend"
FREE_TEXT_RESERVED = frozenset({"sea", "air", "AIR", "UNASSIGNED"})
NAME_PARTICLES = (
    "님",
    "에게",
    "으로",
    "의",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "와",
    "과",
    "도",
    "로",
    "께",
    "측",
    "에서",
    "까지",
    "부터",
    "만",
    "께서",
    "이며",
    "며",
    "이고",
    "고",
    "이나",
    "나",
    "처럼",
    "보다",
    "마다",
    "한테",
    "랑",
    "이랑",
    "하고",
    "조차",
    "뿐",
    "입니다",
    "이다",
    "이에요",
    "예요",
    "요",
)
NAMED_ENTITIES = frozenset({"shipper", "packhouse"})
ENTITY_FIELDS = {"lot": "lot", "lots": "lot", "shippers": "shipper", "packhouses": "packhouse"}
LOT_KEYED_FIELDS = frozenset({"dropped_stops", "lot_pallets_before", "lot_pallets_after"})
SHIPPER_FIELDS = frozenset({"shipper_id", "payer_shipper_id"})
STRUCTURAL_FIELDS = frozenset(
    {
        "id",
        "name",
        "code",
        "mode",
        "kind",
        "type",
        "status",
        "status_after",
        "actor",
        "severity",
        "level",
        "change",
        "product_id",
        "product_ids",
        "container_id",
        "voyage_id",
        "truck_id",
        "depot_id",
        "device_id",
        "terminal",
        "origin",
        "destination",
        "unlocode",
        "vessel_name",
        "plan_id",
        "order_id",
        "replan_id",
        "alternative_id",
        "active_order_id",
        "request_id",
        "scenario_id",
        "counterparty_role",
        "cargo_group",
        "gate_status",
        "gate_mode",
        "billing_rule",
        "baseline_kind",
        "collection_basis",
        "reason_code",
        "reasons",
        "generated_by",
        "consolidator",
        "recipient_kind",
        "sell_window",
        "from_location",
        "to_location",
        "org_type",
        "org_id",
        "user_id",
        "key",
        "unit",
    }
)
MOVED_LOT = re.compile(r"(?<![\w-])(?P<lot>[^\s(]+)\((?P<name>[^\s()]+)(?P<rest> \d+파렛트\))")
SHORT_PART_TEXT = r"[^\s(),:→]+ \d+→\d+(?![\w/→])"
SHORT_RUN = re.compile(rf"(?<![^\s(]){SHORT_PART_TEXT}(?:, {SHORT_PART_TEXT})*")
SHORT_PART = re.compile(r"(?P<name>[^\s(),:→]+)(?P<rest> \d+→\d+)")
LOT_LIST_HEAD = r"(?:대상 출하: |(?<!\w)로트 )"
LOT_LIST_END = r"(?=, |[.:(]|\s|$)"
LOT_MOVE_LIST = re.compile(r"이동: (?P<body>.+?)(?= / |$)")
LOT_MOVE_END = r"(?= \d+파렛트 )"
LOT_ITEM_START = r"(?:^|(?<=, ))"
OTHER_SHIPPER_LOG = {
    "shipper_confirmed": "모든 화주 확인 완료",
    "rejected": "화주 확인 → 이전 확정 계획으로 복귀",
}


def _label(index: int) -> str:
    return str(index + 1)


def _first_voyage(session: PlanSession) -> tuple[str | None, datetime | None]:
    used = {c.voyage_id for c in session.plan.containers if c.used}
    return _earliest_voyage(session, used)


def _earliest_voyage(
    session: PlanSession, voyage_ids: set[str]
) -> tuple[str | None, datetime | None]:
    voyages = sorted(
        (v for v in session.scenario.voyages if v.id in voyage_ids), key=lambda v: (v.etd, v.id)
    )
    if not voyages:
        return None, None
    return voyages[0].vessel_name, voyages[0].etd


def _own_voyage(
    session: PlanSession, assignments: Iterable[Assignment]
) -> tuple[str | None, datetime | None]:
    used = {c.container_id for c in session.plan.containers if c.used}
    voyage_ids = {
        a.voyage_id
        for a in assignments
        if a.mode == "sea" and a.voyage_id is not None and a.container_id in used
    }
    return _earliest_voyage(session, voyage_ids)


def total_pallets(session: PlanSession) -> int:
    return sum(a.pallets for a in session.plan.assignments)


def plan_item(session: PlanSession, forwarder: ForwarderRef | None) -> PlanListItem:
    vessel, departure = _first_voyage(session)
    return PlanListItem(
        plan_id=session.plan_id,
        created_at=session.created_at,
        status=session.status,
        vessel=vessel,
        departure=departure,
        shipper_count=len({lot.shipper_id for lot in session.scenario.lots}),
        total_pallets=total_pallets(session),
        forwarder=forwarder,
    )


def shipper_plan_item(
    session: PlanSession, my_lot_ids: set[str], replan_pending: bool = False
) -> ShipperPlanItem:
    vessel, departure = _own_voyage(
        session, (a for a in session.plan.assignments if a.lot_id in my_lot_ids)
    )
    mine = [lot.id for lot in session.scenario.lots if lot.id in my_lot_ids]
    return ShipperPlanItem(
        plan_id=session.plan_id,
        created_at=session.created_at,
        status=session.status,
        vessel=vessel,
        departure=departure,
        my_lot_ids=mine,
        my_pallets=sum(a.pallets for a in session.plan.assignments if a.lot_id in my_lot_ids),
        total_pallets=total_pallets(session),
        replan_pending=replan_pending,
    )


def replan_pending(state: PlanConfirmationState | None, own_shipper_ids: Iterable[str]) -> bool:
    if state is None:
        return False
    own = set(own_shipper_ids)
    if any(c.status == "rejected" and c.shipper_id not in own for c in state.confirmations):
        return True
    return any(
        c.source != "shipper_rejected" or c.shipper_id not in own for c in state.replan_candidates
    )


def shipper_settlements(
    session: PlanSession, shipper_ids: Iterable[str]
) -> list[ShipperSettlementRow]:
    own = set(shipper_ids)
    vessel, departure = _own_voyage(
        session, (a for a in session.plan.assignments if a.shipper_id in own)
    )
    return [
        ShipperSettlementRow(
            **row.model_dump(),
            plan_id=session.plan_id,
            version=session.version,
            vessel=vessel,
            departure=departure,
        )
        for row in adjusted_settlements(session.plan, session.arrival_inspections)
        if row.shipper_id in own
    ]


def platform_settlement(session: PlanSession, org_names: dict[str, str]) -> PlatformSettlement:
    vessel, departure = _first_voyage(session)
    rows = adjusted_settlements(session.plan, session.arrival_inspections)
    return PlatformSettlement(
        plan_id=session.plan_id,
        vessel=vessel,
        departure=departure,
        total_saving_krw=sum(r.savings for r in rows),
        platform_fee_krw=sum(r.fee for r in rows),
        performance_fee_krw=sum(r.performance_fee for r in rows),
        usage_fee_krw=sum(r.usage_fee for r in rows),
        fee_before_refund_krw=sum(r.fee_before_refund for r in rows),
        quality_refund_krw=sum(r.quality_refund for r in rows),
        pilot=any(r.pilot for r in rows),
        shippers=[
            PlatformShipperShare(
                shipper_id=r.shipper_id,
                shipper_org_name=org_names.get(r.shipper_id),
                saving_krw=r.savings,
                fee_krw=r.fee,
                billing_rule=r.billing_rule,
                performance_fee_krw=r.performance_fee,
                usage_fee_krw=r.usage_fee,
                fee_before_refund_krw=r.fee_before_refund,
                quality_refund_krw=r.quality_refund,
                baseline_kind=r.baseline_kind,
                baseline_label=r.baseline_label,
                savings_baseline_label=r.savings_baseline_label,
                pilot=r.pilot,
            )
            for r in rows
        ],
    )


def forwarders_of(plan_ids: list[str]) -> dict[str, ForwarderRef]:
    if not plan_ids:
        return {}
    with database.begin() as db:
        rows = db.execute(
            select(PlanForwarderRow.plan_id, OrganizationRow.id, OrganizationRow.name)
            .join(OrganizationRow, OrganizationRow.id == PlanForwarderRow.forwarder_org_id)
            .where(PlanForwarderRow.plan_id.in_(plan_ids))
            .order_by(PlanForwarderRow.created_at, OrganizationRow.id)
        ).all()
    found: dict[str, ForwarderRef] = {}
    for plan_id, org_id, name in rows:
        found.setdefault(plan_id, ForwarderRef(org_id=org_id, org_name=name))
    return found


def _token_pattern(token: str) -> str:
    escaped = re.escape(token)
    if token.isascii():
        return f"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
    return _name_pattern(token)


def _lot_pattern(token: str) -> str:
    escaped = re.escape(token)
    if token.isascii():
        return f"(?<![A-Za-z0-9_-]){escaped}(?![A-Za-z0-9_-])"
    return rf"(?<![\w-]){escaped}(?![\w-])"


def _name_pattern(token: str) -> str:
    escaped = re.escape(token)
    single = len(token.split()) == 1
    left = r"(?<![\w-])" if single else r"(?<!\w)"
    if token[-1].isascii() and token[-1].isalnum():
        right = r"(?![A-Za-z0-9_-])" if single else r"(?![A-Za-z0-9_])"
        return f"{left}{escaped}{right}"
    if not single:
        return f"{left}{escaped}"
    particles = "|".join(NAME_PARTICLES)
    return rf"{left}{escaped}(?=(?:{particles})*(?!\w))"


def _model_classes(base: type[BaseModel]) -> Iterator[type[BaseModel]]:
    for sub in base.__subclasses__():
        yield sub
        yield from _model_classes(sub)


def _literal_values(annotation: Any) -> set[str]:
    if typing.get_origin(annotation) is typing.Literal:
        return {v for v in typing.get_args(annotation) if isinstance(v, str)}
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return {m.value for m in annotation if isinstance(m.value, str)}
    found: set[str] = set()
    for arg in typing.get_args(annotation):
        found |= _literal_values(arg)
    return found


@cache
def enum_literals() -> frozenset[str]:
    found: set[str] = set()
    for model in _model_classes(BaseModel):
        if not model.__module__.startswith(MODEL_PACKAGE):
            continue
        for field in model.model_fields.values():
            found |= _literal_values(field.annotation)
    return frozenset(found)


def _lot_field(key: str) -> bool:
    return key == "lot_id" or key.endswith("lot_ids") or key.startswith("lot_ids_")


def _shipper_field(key: str) -> bool:
    return key in SHIPPER_FIELDS or key.endswith("shipper_ids")


def _packhouse_field(key: str) -> bool:
    return key == "packhouse_id" or key.endswith("packhouse_ids") or key.startswith("packhouses_")


def _stop_field(key: str) -> bool:
    return key == "stop_id" or key.endswith("stop_ids")


def _reserved_words(scenario: Scenario) -> set[str]:
    words = set(FREE_TEXT_RESERVED) | enum_literals()
    words |= {c.id for c in scenario.containers}
    words |= {t.id for t in scenario.trucks}
    for voyage in scenario.voyages:
        words |= {voyage.id, voyage.vessel_name, voyage.terminal}
    for product in scenario.products:
        words |= {product.id, product.name}
    for port in scenario.ports:
        words |= {port.id, port.unlocode, port.name}
    for packhouse in scenario.packhouses:
        words |= {packhouse.id, packhouse.name}
    for shipper in scenario.shippers:
        words |= {shipper.id, shipper.name, short_name(shipper.name)}
    return words


def _free_text_lot(lot_id: str, reserved: set[str]) -> bool:
    if lot_id in reserved or len(lot_id) < MIN_TEXT_LOT_LENGTH:
        return False
    if any(c.isalpha() for c in lot_id):
        return True
    return len(lot_id) >= MIN_NUMERIC_TEXT_LOT_LENGTH and any(c.isdigit() for c in lot_id)


LotLedger = Mapping[str, str | None]


def _note_lot(ledger: dict[str, str | None], lot_id: Any, owner: Any) -> None:
    if not isinstance(lot_id, str) or not lot_id:
        return
    if ledger.get(lot_id) is None:
        ledger[lot_id] = owner if isinstance(owner, str) and owner else None


def _collect_lots(value: Any, ledger: dict[str, str | None]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_lots(item, ledger)
        return
    if not isinstance(value, dict):
        return
    owner = value.get("shipper_id") or value.get("payer_shipper_id")
    for key, item in value.items():
        if key == "lot" and isinstance(item, dict):
            _note_lot(ledger, item.get("id"), item.get("shipper_id"))
            continue
        if key == "lot_id":
            _note_lot(ledger, item, owner)
        elif key.endswith("lot_ids") and isinstance(item, list):
            for lot_id in item:
                _note_lot(ledger, lot_id, owner if key == "lot_ids" else None)
        elif key == "dropped_stops" and isinstance(item, dict):
            for lot_id in item:
                _note_lot(ledger, lot_id, None)
        else:
            _collect_lots(item, ledger)


def _stored_lot_shippers(lot_ids: list[str]) -> dict[str, str]:
    if not lot_ids:
        return {}
    with database.begin() as db:
        rows = db.execute(select(LotRow.id, LotRow.shipper_id).where(LotRow.id.in_(lot_ids)))
        return {lot_id: shipper_id for lot_id, shipper_id in rows}


def lot_ledger(
    session: PlanSession,
    orders: Iterable[ExecutionOrder] = (),
    own_shipper_ids: Iterable[str] = (),
) -> dict[str, str | None]:
    own = set(own_shipper_ids)
    ledger: dict[str, str | None] = dict.fromkeys(session.registered_lot_ids)
    for entry in sorted(session.history, key=lambda e: e.seq):
        if isinstance(entry.event, AddLotEvent):
            _note_lot(ledger, entry.event.lot.id, entry.event.lot.shipper_id)
    for lot in session.scenario.lots:
        ledger[lot.id] = lot.shipper_id
    _collect_lots(session.model_dump(mode="json"), ledger)
    for order in orders:
        _collect_lots(order.model_dump(mode="json"), ledger)
    unresolved = [lot_id for lot_id, owner in ledger.items() if owner is None]
    for lot_id, owner in _stored_lot_shippers(unresolved).items():
        if owner not in own:
            ledger[lot_id] = owner
    return ledger


def _scenario_ledger(scenario: Scenario) -> dict[str, str | None]:
    return {lot.id: lot.shipper_id for lot in scenario.lots}


class Anonymizer:
    def __init__(
        self,
        scenario: Scenario,
        own_shipper_ids: Iterable[str],
        lots: LotLedger | None = None,
    ) -> None:
        own = set(own_shipper_ids)
        ledger = _scenario_ledger(scenario) if lots is None else dict(lots)
        own_packhouses = {s.packhouse_id for s in scenario.shippers if s.id in own}
        own_packhouses |= {
            lot.packhouse_id
            for lot in scenario.lots
            if lot.shipper_id in own and lot.packhouse_id is not None
        }
        packhouses = {p.id: p for p in scenario.packhouses}
        listed = [s.id for s in scenario.shippers]
        with_lots = {sid for sid in ledger.values() if sid is not None}
        shipper_ids = [sid for sid in listed if sid in with_lots]
        shipper_ids += sorted(with_lots - set(listed))
        shipper_ids += [sid for sid in listed if sid not in with_lots]
        shippers = {s.id: s for s in scenario.shippers}
        self.own = own
        self.owners: dict[str, str | None] = ledger
        self.order = {sid: index for index, sid in enumerate(listed)}
        self.short_names = {s.id: short_name(s.name) for s in scenario.shippers}
        self.hidden_packhouses: set[str] = set()
        self.own_lot_ids = {lot_id for lot_id, sid in ledger.items() if sid in own}
        own_names = {s.name for s in scenario.shippers if s.id in own}
        own_names |= {p.name for p in scenario.packhouses if p.id in own_packhouses}
        self._shippers: dict[str, str] = {}
        self._packhouses: dict[str, str] = {}
        self._names: dict[str, str] = {name: name for name in own_names}
        others = [sid for sid in shipper_ids if sid not in own]
        self.labels: dict[str | None, str] = {None: UNKNOWN_OWNER_LABEL}
        for index, sid in enumerate(others):
            label = _label(index)
            self.labels[sid] = label
            self._shippers[sid] = f"anon-{label}"
            shipper = shippers.get(sid)
            if shipper is None:
                continue
            if shipper.name not in own_names:
                self._names[shipper.name] = f"{OTHER_SHIPPER_LABEL} {label}"
            sites = list(
                dict.fromkeys(
                    [
                        shipper.packhouse_id,
                        *(
                            lot.packhouse_id
                            for lot in scenario.lots
                            if lot.shipper_id == sid and lot.packhouse_id is not None
                        ),
                    ]
                )
            )
            for site_index, site in enumerate(sites):
                if site in own_packhouses:
                    continue
                anon_ph = (
                    f"anon-ph-{label}" if site_index == 0 else f"anon-ph-{label}-{site_index + 1}"
                )
                self._packhouses[site] = anon_ph
                self.hidden_packhouses.add(anon_ph)
                ph = packhouses.get(site)
                if ph is not None and ph.name not in own_names:
                    self._names[ph.name] = f"{OTHER_SHIPPER_LABEL} {label} 선과장"
        self._lots: dict[str, str] = {}
        counts: dict[str, int] = {}
        for lot_id, sid in ledger.items():
            if lot_id in self.own_lot_ids:
                self._lots[lot_id] = lot_id
                continue
            label = self.labels.get(sid, UNKNOWN_OWNER_LABEL)
            counts[label] = counts.get(label, 0) + 1
            self._lots[lot_id] = f"anon-{label}-{counts[label]}"
        self._shorts = self._short_labels(scenario)
        self._reserved = _reserved_words(scenario)
        self._compile()

    def _short_labels(self, scenario: Scenario) -> dict[str, str]:
        by_short: dict[str, set[str]] = {}
        for shipper in scenario.shippers:
            by_short.setdefault(short_name(shipper.name), set()).add(shipper.id)
        labels: dict[str, str] = {}
        for short, sids in by_short.items():
            if not short or sids <= self.own:
                continue
            label = self.label(next(iter(sids))) if len(sids) == 1 else None
            labels[short] = label or OTHER_SHIPPER_LABEL
        return labels

    def _compile(self) -> None:
        tokens: dict[str, str] = {}
        patterns: dict[str, str] = {}
        for lot_id, anon in self._lots.items():
            if _free_text_lot(lot_id, self._reserved):
                tokens[lot_id] = anon
                patterns[lot_id] = _lot_pattern(lot_id)
        for source in (self._shippers, self._packhouses):
            for value, anon in source.items():
                tokens[value] = anon
                patterns[value] = _token_pattern(value)
        for name, anon in self._names.items():
            if name:
                tokens[name] = anon
                patterns[name] = _name_pattern(name)
        self._tokens = tokens
        ordered = sorted(tokens, key=len, reverse=True)
        self._pattern = re.compile("|".join(patterns[t] for t in ordered)) if ordered else None
        self._list_region: re.Pattern[str] | None = None
        self._list_item: re.Pattern[str] | None = None
        self._move_item: re.Pattern[str] | None = None
        if any(anon != lot_id for lot_id, anon in self._lots.items()):
            known = "|".join(re.escape(t) for t in sorted(self._lots, key=len, reverse=True) if t)
            item = rf"(?:(?:{known}){LOT_LIST_END}|[^\s,]+)"
            self._list_region = re.compile(rf"{LOT_LIST_HEAD}(?P<body>{item}(?:, {item})*)")
            self._list_item = re.compile(rf"{LOT_ITEM_START}(?:{known}){LOT_LIST_END}")
            self._move_item = re.compile(rf"{LOT_ITEM_START}(?:{known}){LOT_MOVE_END}")

    def extended(self, lots: Mapping[str, str]) -> "Anonymizer":
        if not lots:
            return self
        clone = copy.copy(self)
        clone._lots = {**self._lots, **lots}
        clone._compile()
        return clone

    def text(self, value: str, context: list[str] | None = None) -> str:
        value = MOVED_LOT.sub(self._moved, value)
        value = SHORT_RUN.sub(lambda m: self._run(m.group(0), context or []), value)
        value = self._listed_lots(value)
        if self._pattern is None:
            return value
        return self._pattern.sub(lambda m: self._tokens[m.group(0)], value)

    def _listed_lots(self, value: str) -> str:
        region, listed, moved = self._list_region, self._list_item, self._move_item
        if region is None or listed is None or moved is None:
            return value
        value = region.sub(lambda m: self._in_body(m, listed), value)
        return LOT_MOVE_LIST.sub(lambda m: self._in_body(m, moved), value)

    def _in_body(self, found: re.Match[str], item: re.Pattern[str]) -> str:
        head = found.group(0)[: found.start("body") - found.start()]
        return head + item.sub(lambda m: self._lots[m.group(0)], found.group("body"))

    def _moved(self, found: re.Match[str]) -> str:
        lot_id = found.group("lot")
        if lot_id not in self.owners:
            return found.group(0)
        owner = self.owners[lot_id]
        name = found.group("name")
        expected = self.short_names.get(owner) if owner is not None else None
        if expected is None and lot_id in self._reserved:
            return found.group(0)
        if expected is not None and name != expected:
            return found.group(0)
        label = self.label(owner) or name
        return f"{self.lot(lot_id)}({label}{found.group('rest')}"

    def _run(self, run: str, context: list[str]) -> str:
        parts = [SHORT_PART.fullmatch(part) for part in run.split(", ")]
        names = [(p.group("name"), p.group("rest")) for p in parts if p is not None]
        owners = sorted(
            {self.owners.get(lot_id) or lot_id for lot_id in context},
            key=lambda sid: (self.order.get(sid, len(self.order)), sid),
        )
        if len(owners) == len(names) == len(parts) and all(
            self.short_names.get(sid, name) == name
            for sid, (name, _) in zip(owners, names, strict=True)
        ):
            return ", ".join(
                f"{self.label(sid) or name}{rest}"
                for sid, (name, rest) in zip(owners, names, strict=True)
            )
        return ", ".join(f"{self._shorts.get(name, name)}{rest}" for name, rest in names)

    def knows(self, value: str) -> bool:
        return value in self._lots

    def lot(self, value: str) -> str:
        return self._lots.get(value, value)

    def label(self, shipper_id: str | None) -> str | None:
        if shipper_id in self.own:
            return None
        return f"{OTHER_SHIPPER_LABEL} {self.labels.get(shipper_id, UNKNOWN_OWNER_LABEL)}"

    def _stop(self, value: str) -> str:
        if not value.startswith(STOP_PREFIX):
            return value
        rest = value[len(STOP_PREFIX) :]
        for ph in sorted(self._packhouses, key=len, reverse=True):
            if rest == ph or rest.startswith(f"{ph}-"):
                return f"{STOP_PREFIX}{self._packhouses[ph]}{rest[len(ph) :]}"
        return value

    def _string(
        self, key: str | None, value: str, entity: str | None, context: list[str] | None
    ) -> str:
        if key is None:
            return self.text(value, context)
        if key == "id" and entity is not None:
            return self._entity_ids(entity).get(value, value)
        if key == "name" and entity in NAMED_ENTITIES:
            return self._names.get(value) or value
        if _lot_field(key):
            return self.lot(value)
        if _shipper_field(key):
            return self._shippers.get(value, value)
        if _packhouse_field(key):
            return self._packhouses.get(value, value)
        if _stop_field(key):
            return self._stop(value)
        if key == "shipper_name":
            return self._names.get(value) or self.text(value)
        if key in STRUCTURAL_FIELDS or value in enum_literals():
            return value
        return self.text(value, context)

    def _entity_ids(self, entity: str) -> Mapping[str, str]:
        if entity == "lot":
            return self._lots
        if entity == "shipper":
            return self._shippers
        return self._packhouses

    def _field(
        self,
        key: str | None,
        value: Any,
        entity: str | None = None,
        context: list[str] | None = None,
    ) -> Any:
        if isinstance(value, list):
            return [self._field(key, item, entity, context) for item in value]
        if isinstance(value, dict):
            if key in LOT_KEYED_FIELDS:
                inner = "stop_id" if key == "dropped_stops" else None
                return {self.lot(k): self._field(inner, v) for k, v in value.items()}
            child = ENTITY_FIELDS.get(key) if key is not None else None
            lot_ids = value.get("lot_ids")
            scope = lot_ids if isinstance(lot_ids, list) else None
            return {k: self._field(k, v, child, scope) for k, v in value.items()}
        if isinstance(value, str):
            return self._string(key, value, entity, context)
        return value

    def walk(self, value: Any) -> Any:
        return self._field(None, value)

    def session(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.walk(payload)
        scenario = result.get("scenario") or {}
        if "description" in scenario:
            scenario["description"] = ""
        for packhouse in scenario.get("packhouses", []):
            if packhouse.get("id") in self.hidden_packhouses:
                packhouse.update(region="", lat=None, lon=None)
        return result


def shipper_anonymizer(
    session: PlanSession,
    own_shipper_ids: Iterable[str],
    orders: Iterable[ExecutionOrder] = (),
) -> Anonymizer:
    own = set(own_shipper_ids)
    return Anonymizer(session.scenario, own, lot_ledger(session, orders, own))


def _without_total_cost(summary: str) -> str:
    return TOTAL_COST_CLAUSE.sub("입니다.", summary)


def _scope_explanations(payload: dict[str, Any], own: set[str]) -> None:
    for entry in payload["history"]:
        explanation = entry.get("explanation")
        if explanation is None:
            continue
        explanation["operator_summary"] = _without_total_cost(explanation["operator_summary"])
        explanation["shipper_messages"] = [
            m for m in explanation["shipper_messages"] if m["shipper_id"] in own
        ]


def _cost_total(costs: list[dict[str, Any]], name: str) -> int:
    return sum((c.get(name) or {}).get("total", 0) for c in costs)


def _own_container(container: dict[str, Any], own: set[str]) -> dict[str, Any] | None:
    rows = [r for r in container["shippers"] if r["shipper_id"] in own]
    if not rows:
        return None
    return {
        **container,
        "shippers": rows,
        "total_savings": sum(r["savings"] for r in rows),
        "total_fee": sum(r["fee"] for r in rows),
        "total_shipper_gain": sum(r["shipper_gain"] for r in rows),
    }


def _scope_settlements(payload: dict[str, Any], own: set[str]) -> None:
    plan = payload["plan"]
    rows = [s for s in plan["settlements"] if s["shipper_id"] in own]
    costs = [c for c in plan["shipper_costs"] if c["shipper_id"] in own]
    containers = [_own_container(c, own) for c in plan["container_settlements"]]
    operational_baseline = _cost_total(costs, "operational_baseline")
    operational = _cost_total(costs, "operational")
    collections = [c for c in plan["lot_collections"] if c["shipper_id"] in own]
    if plan["total_collection_route"] is not None:
        plan["total_collection_route"] = sum(
            (c.get("operational") or {}).get("collection", 0) for c in costs
        )
    plan.update(
        lot_collections=collections,
        total_collection_estimate=sum(
            (c.get("operational") or {}).get("collection_estimate") or 0 for c in costs
        ),
        settlements=rows,
        shipper_costs=costs,
        container_settlements=[c for c in containers if c is not None],
        total_baseline_cost=sum(s["baseline_cost"] for s in rows),
        total_shared_cost=sum(s["shared_cost"] for s in rows),
        total_savings=sum(s["savings"] for s in rows),
        total_fee=sum(s["fee"] for s in rows),
        total_operational_baseline_cost=operational_baseline,
        total_operational_cost=operational,
        total_operational_savings=operational_baseline - operational,
        objective=None,
    )
    for entry in payload["history"]:
        for name in HISTORY_MONEY:
            entry[name] = None


def _operational_inspections(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**{name: row[name] for name in INSPECTION_FIELDS}, "excess_loss_value": None, "note": None}
        for row in rows
    ]


def _scope_tariffs(payload: dict[str, Any], session: PlanSession, own: set[str]) -> None:
    products = {lot.product_id for lot in session.scenario.lots if lot.shipper_id in own}
    plan = payload["plan"]
    plan["settlement_tariffs"] = [
        t for t in plan["settlement_tariffs"] if t["product_id"] in products
    ]


def _exclusive(route: dict[str, Any], own_lots: set[str]) -> bool:
    lot_ids = {lot_id for stop in route["stops"] for lot_id in stop["lot_ids"]}
    return bool(lot_ids) and lot_ids <= own_lots


def _allowed(item: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {key: value if key in fields else None for key, value in item.items()}


def _hidden_route(route: dict[str, Any]) -> dict[str, Any]:
    scoped = _allowed(route, ROUTE_FIELDS)
    scoped["stops"] = [_allowed(stop, STOP_FIELDS) for stop in route["stops"]]
    return scoped


def _scope_routes(payload: dict[str, Any], own_lots: set[str]) -> None:
    for voyage in payload["routes"]:
        pickup = voyage["pickup"]
        routes = pickup["routes"]
        if all(_exclusive(route, own_lots) for route in routes):
            continue
        voyage["pickup"] = {
            **_allowed(pickup, PICKUP_FIELDS),
            "routes": [
                route if _exclusive(route, own_lots) else _hidden_route(route) for route in routes
            ],
        }


def _scope_parties(payload: dict[str, Any], own: set[str]) -> None:
    scenario = payload["scenario"]
    scenario["description"] = ""
    kept = (
        own
        | {lot["shipper_id"] for lot in scenario["lots"]}
        | {a["shipper_id"] for a in payload["plan"]["assignments"]}
    )
    scenario["shippers"] = [s for s in scenario["shippers"] if s["id"] in kept]
    sites = {s["packhouse_id"] for s in scenario["shippers"]}
    sites |= {lot["packhouse_id"] for lot in scenario["lots"] if lot.get("packhouse_id")}
    scenario["packhouses"] = [p for p in scenario["packhouses"] if p["id"] in sites]


def anonymized_session(
    session: PlanSession,
    own_shipper_ids: Iterable[str],
    orders: Iterable[ExecutionOrder] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    own = set(own_shipper_ids)
    anonymizer = shipper_anonymizer(session, own, orders)
    payload = {**session.model_dump(mode="json"), **(extra or {})}
    _scope_settlements(payload, own)
    _scope_tariffs(payload, session, own)
    _scope_explanations(payload, own)
    _scope_routes(payload, anonymizer.own_lot_ids)
    payload["arrival_inspections"] = [
        row for row in payload["arrival_inspections"] if row["shipper_id"] in own
    ]
    _scope_parties(payload, own)
    return anonymizer.session(payload)


def shipper_container_numbers(
    session: PlanSession,
    own_shipper_ids: Iterable[str],
    entered: Mapping[str, EnteredNumbers],
) -> list[ShipperContainerNumbers]:
    own = set(own_shipper_ids)
    mine = {
        a.container_id
        for a in session.plan.assignments
        if a.mode == "sea" and a.shipper_id in own and a.container_id is not None
    }
    numbers: list[ShipperContainerNumbers] = []
    for load in session.plan.containers:
        if not load.used or load.container_id not in mine:
            continue
        actual = entered.get(load.container_id, EnteredNumbers())
        numbers.append(
            ShipperContainerNumbers(
                container_id=load.container_id,
                voyage_id=load.voyage_id,
                container_no=actual.container_no
                or mock_container_no(session.plan_id, load.container_id),
                seal_no=actual.seal_no or mock_seal_no(session.plan_id, load.container_id),
                mock=actual.container_no is None or actual.seal_no is None,
                container_no_mock=actual.container_no is None,
                seal_no_mock=actual.seal_no is None,
            )
        )
    return numbers


def shipper_lot_ids(session: PlanSession, own_shipper_ids: Iterable[str]) -> set[str]:
    own = set(own_shipper_ids)
    return {lot.id for lot in session.scenario.lots if lot.shipper_id in own}


def shipper_genset_instructions(
    session: PlanSession,
    own_shipper_ids: Iterable[str],
    entered: Mapping[str, EnteredNumbers],
) -> list[GensetInstruction]:
    own = set(own_shipper_ids)
    card = build_execution_card(
        session.plan_id, session.version, session.scenario, session.plan, entered=dict(entered)
    )
    return genset_instructions(
        power_plan(session, shipper_ids=own).candidates,
        card,
        visible_lot_ids=shipper_lot_ids(session, own),
    )


def related_to_shipper(order: ExecutionOrder, own: set[str], my_lots: set[str]) -> bool:
    shippers = {c.shipper_id for c in order.shipper_confirmations} | set(order.notified_shipper_ids)
    return bool(own & shippers) or any(my_lots & set(r.lot_ids) for r in order.requests)


def _reply_request_id(entry: OrderLogEntry) -> str | None:
    if entry.actor != "forwarder":
        return None
    return entry.action.split(" ", 1)[0]


def _scoped_log(
    log: list[OrderLogEntry], own: set[str], hidden_requests: set[str]
) -> list[OrderLogEntry]:
    scoped: list[OrderLogEntry] = []
    for entry in log:
        if _reply_request_id(entry) in hidden_requests:
            continue
        if entry.actor != "shipper" or entry.shipper_id in own:
            scoped.append(entry)
            continue
        action = OTHER_SHIPPER_LOG.get(entry.status_after)
        if action is not None:
            scoped.append(
                entry.model_copy(update={"action": action, "shipper_id": None, "note": None})
            )
    return scoped


def _order_view(
    anonymizer: Anonymizer, order: ExecutionOrder, own: set[str], my_lots: set[str]
) -> dict[str, Any]:
    requests = [r for r in order.requests if my_lots & set(r.lot_ids)]
    visible = {r.request_id for r in requests}
    hidden = {r.request_id for r in order.requests} - visible
    trimmed = order.model_copy(
        update={
            "shipper_confirmations": [
                c for c in order.shipper_confirmations if c.shipper_id in own
            ],
            "notified_shipper_ids": [s for s in order.notified_shipper_ids if s in own],
            "requests": requests,
            "log": _scoped_log(order.log, own, hidden),
        }
    )
    payload = trimmed.model_dump(mode="json")
    for action in payload["automatic"]:
        if action["kind"] == SETTLEMENT_ACTION:
            action["description"] = _own_settlement_text(order_charges(order), own)
    cancellation = payload.get("cancellation")
    if cancellation is not None:
        _scope_charge(cancellation, own)
    payload["cancellation_charges"] = [
        _scope_charge(charge, own)
        for charge in _charges_payload(order)
        if _charge_concerns(charge, own)
    ]
    return anonymizer.walk(payload)


def _charge_concerns(charge: dict[str, Any], own: set[str]) -> bool:
    return charge["payer_shipper_id"] in own or any(
        t["shipper_id"] in own for t in charge["transfers"]
    )


def _scope_charge(charge: dict[str, Any], own: set[str]) -> dict[str, Any]:
    if charge["payer_shipper_id"] not in own:
        charge["fee"] = None
        charge["consolidator_amount"] = None
    charge["transfers"] = [t for t in charge["transfers"] if t["shipper_id"] in own]
    return charge


def _charges_payload(order: ExecutionOrder) -> list[dict[str, Any]]:
    return [charge.model_dump(mode="json") for charge in order_charges(order)]


def order_charges(order: ExecutionOrder) -> list[CancellationCharge]:
    if order.cancellation_charges:
        return order.cancellation_charges
    if order.cancellation is not None and order.cancellation.fee > 0:
        return [order.cancellation]
    return []


def shipper_order_view(
    session: PlanSession,
    order: ExecutionOrder,
    own_shipper_ids: Iterable[str],
    orders: Iterable[ExecutionOrder] = (),
) -> dict[str, Any]:
    own = set(own_shipper_ids)
    known = [*orders]
    if all(o.order_id != order.order_id for o in known):
        known.append(order)
    anonymizer = shipper_anonymizer(session, own, known)
    my_lots = shipper_lot_ids(session, own) | anonymizer.own_lot_ids
    return _order_view(anonymizer, order, own, my_lots)


def _own_settlement_text(charges: Iterable[CancellationCharge], own: set[str]) -> str:
    charged = [c for c in charges if c.fee > 0]
    paid = sum(c.fee for c in charged if c.payer_shipper_id in own)
    received = [t.amount for c in charged for t in c.transfers if t.shipper_id in own]
    parts = [SETTLEMENT_TITLE]
    if paid:
        parts.append(f"취소 부담금 {paid:,}원 부담")
    if received:
        parts.append(f"취소 부담금 {sum(received):,}원 보전")
    return ". ".join(parts)


def anonymized_orders(
    session: PlanSession, orders: list[ExecutionOrder], own_shipper_ids: Iterable[str]
) -> list[dict[str, Any]]:
    own = set(own_shipper_ids)
    anonymizer = shipper_anonymizer(session, own, orders)
    my_lots = shipper_lot_ids(session, own) | anonymizer.own_lot_ids
    return [
        _order_view(anonymizer, order, own, my_lots)
        for order in orders
        if related_to_shipper(order, own, my_lots)
    ]


def _own_packhouses(session: PlanSession, own: set[str]) -> set[str]:
    return {s.packhouse_id for s in session.scenario.shippers if s.id in own} | {
        lot.packhouse_id
        for lot in session.scenario.lots
        if lot.shipper_id in own and lot.packhouse_id is not None
    }


def shipper_route_changes(
    session: PlanSession,
    changes: Iterable[RouteChange],
    own_shipper_ids: Iterable[str],
    orders: Iterable[ExecutionOrder] = (),
) -> list[ShipperRouteChange]:
    own = set(own_shipper_ids)
    anonymizer = shipper_anonymizer(session, own, orders)
    own_lots = shipper_lot_ids(session, own) | anonymizer.own_lot_ids
    own_packhouses = _own_packhouses(session, own)
    scoped: list[ShipperRouteChange] = []
    for change in changes:
        lots = set(change.lot_ids_before) | set(change.lot_ids_after)
        stops = set(change.packhouses_before) | set(change.packhouses_after)
        if not (lots & own_lots or stops & own_packhouses):
            continue
        item = change.model_dump(mode="json")
        for name in ROUTE_CHANGE_PACKHOUSES:
            item[name] = [ph for ph in item[name] if ph in own_packhouses]
        for name in ROUTE_CHANGE_LOTS:
            item[name] = [lot_id for lot_id in item[name] if lot_id in own_lots]
        for name in ROUTE_CHANGE_MONEY:
            item[name] = None
        scoped.append(ShipperRouteChange.model_validate(anonymizer.walk(item)))
    return scoped


def shipper_fixed_reasons(
    session: PlanSession,
    reasons: Iterable[FixedLot],
    own_shipper_ids: Iterable[str],
    orders: Iterable[ExecutionOrder] = (),
) -> list[FixedLot]:
    own = set(own_shipper_ids)
    anonymizer = shipper_anonymizer(session, own, orders)
    own_lots = shipper_lot_ids(session, own) | anonymizer.own_lot_ids
    return [
        FixedLot.model_validate(anonymizer.walk(reason.model_dump(mode="json")))
        for reason in reasons
        if reason.lot_id in own_lots
    ]


def without_settlement(session: PlanSession) -> dict[str, Any]:
    payload = session.model_dump(mode="json")
    plan = payload["plan"]
    for name in MONEY_LISTS:
        plan[name] = []
    for name in MONEY_TOTALS:
        plan[name] = None
    plan["objective"] = None
    for entry in payload["history"]:
        for name in HISTORY_MONEY:
            entry[name] = None
    _scope_explanations(payload, set())
    payload["arrival_inspections"] = _operational_inspections(payload["arrival_inspections"])
    _scope_parties(payload, set())
    return payload


def forwarder_order_view(order: ExecutionOrder) -> ForwarderOrder:
    payload = order.model_dump(mode="json")
    for action in payload["automatic"]:
        if action["kind"] == SETTLEMENT_ACTION:
            action["description"] = SETTLEMENT_TITLE
    for confirmation in payload["shipper_confirmations"]:
        for name in CONFIRMATION_MONEY:
            confirmation[name] = None
    cancellation = payload.get("cancellation")
    if cancellation is not None:
        _without_charge_money(cancellation)
    payload["cancellation_charges"] = _charges_payload(order)
    for charge in payload["cancellation_charges"]:
        _without_charge_money(charge)
    return ForwarderOrder.model_validate(payload)


def _without_charge_money(charge: dict[str, Any]) -> None:
    for name in CANCELLATION_MONEY:
        charge[name] = None
    charge["transfers"] = [
        {**transfer, "share_increase": None, "amount": None} for transfer in charge["transfers"]
    ]


def _hidden_handler(handler: dict[str, Any] | None) -> dict[str, Any] | None:
    if handler is None:
        return None
    return {"org_type": handler["org_type"], "org_id": None, "user_id": None}


class ShipperColdchainView:
    def __init__(self, anonymizer: Anonymizer | None, own_lot_ids: Iterable[str] = ()) -> None:
        self.anonymizer = anonymizer
        self.own_lot_ids = set(own_lot_ids)
        if anonymizer is not None:
            self.own_lot_ids |= anonymizer.own_lot_ids

    def _walk(self, payload: Any) -> Any:
        if self.anonymizer is None:
            return payload
        return self.anonymizer.walk(payload)

    def _action_payload(self, action: dict[str, Any]) -> dict[str, Any]:
        scoped = {
            **action,
            "accepted_by": _hidden_handler(action.get("accepted_by")),
            "done_by": _hidden_handler(action.get("done_by")),
        }
        if self.anonymizer is None:
            scoped["lot_ids"] = [x for x in scoped["lot_ids"] if x in self.own_lot_ids]
            return scoped
        unknown = {
            x: f"anon-{UNKNOWN_OWNER_LABEL}"
            for x in scoped["lot_ids"]
            if x not in self.own_lot_ids and not self.anonymizer.knows(x)
        }
        return self.anonymizer.extended(unknown).walk(scoped)

    def _alert_payload(self, alert: dict[str, Any]) -> dict[str, Any]:
        action = alert.get("action")
        scoped = self._walk({**alert, "action": None})
        scoped["action"] = None if action is None else self._action_payload(action)
        return scoped

    def action(self, action: ColdchainAction) -> ColdchainAction:
        return ColdchainAction.model_validate(self._action_payload(action.model_dump(mode="json")))

    def alert(self, alert: Alert) -> Alert:
        return Alert.model_validate(self._alert_payload(alert.model_dump(mode="json")))

    def state(self, state: TwinState) -> TwinState:
        return state.model_copy(
            update={
                "actions": [
                    ColdchainAction.model_validate(self._action_payload(a.model_dump(mode="json")))
                    for a in state.actions
                ],
                "alerts": [
                    Alert.model_validate(self._alert_payload(a.model_dump(mode="json")))
                    for a in state.alerts
                ],
                "loss": LossSummary.model_validate(self._walk(state.loss.model_dump(mode="json"))),
            }
        )


def shipper_coldchain_view(
    session: PlanSession | None,
    own_shipper_ids: Iterable[str],
    orders: Iterable[ExecutionOrder] = (),
    own_lot_ids: Iterable[str] = (),
) -> ShipperColdchainView:
    if session is None:
        return ShipperColdchainView(None, own_lot_ids)
    return ShipperColdchainView(shipper_anonymizer(session, own_shipper_ids, orders), own_lot_ids)
