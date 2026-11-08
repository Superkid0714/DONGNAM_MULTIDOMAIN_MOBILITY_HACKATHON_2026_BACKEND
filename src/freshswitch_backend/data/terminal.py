from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from freshswitch_backend.domain.models import KST, KstDatetime, Voyage

TERMINAL_SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "terminal"


class PortCall(BaseModel):
    seq: int
    code: str
    name: str


class HongKongCall(BaseModel):
    order_after_busan: int
    via: list[str]


class TerminalInfo(BaseModel):
    code: str
    name: str
    mof_terminal: str
    scenario_terminal: str


class TerminalVessel(BaseModel):
    role: Literal["primary", "alternate"]
    vessel_name: str
    call_sign: str
    call_sign_source: str
    terminal_vessel_code: str
    operator: str
    voyage: str
    route: str
    berth: str
    berthing_side: str
    etb: KstDatetime | None = None
    etd: KstDatetime
    cy_cutoff: KstDatetime | None = None
    next_port: str | None = None
    next_port_basis: str
    port_rotation: list[PortCall]
    port_rotation_url: str
    hong_kong_call: HongKongCall | None = None
    transit_days_to_hkhkg: float | None = None
    gaps: list[str]
    terminal_row: dict


class TerminalSnapshot(BaseModel):
    source: str
    label: str
    terminal: TerminalInfo
    source_url: str
    fetched_at: datetime
    query: dict
    vessels: list[TerminalVessel]


class TerminalSailing(BaseModel):
    snapshot: TerminalSnapshot
    vessel: TerminalVessel


@lru_cache
def load_terminal_snapshots(root: Path = TERMINAL_SNAPSHOT_DIR) -> tuple[TerminalSnapshot, ...]:
    return tuple(
        TerminalSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(root.glob("*.json"))
    )


def hong_kong_sailings(
    start: date | None = None, end: date | None = None, root: Path = TERMINAL_SNAPSHOT_DIR
) -> list[TerminalSailing]:
    sailings = [
        TerminalSailing(snapshot=snapshot, vessel=vessel)
        for snapshot in load_terminal_snapshots(root)
        for vessel in snapshot.vessels
        if vessel.hong_kong_call is not None
        and (start is None or vessel.etd.date() >= start)
        and (end is None or vessel.etd.date() <= end)
    ]
    return sorted(sailings, key=lambda s: s.vessel.etd)


def find_sailing(call_sign: str, root: Path = TERMINAL_SNAPSHOT_DIR) -> TerminalSailing | None:
    return next(
        (s for s in hong_kong_sailings(root=root) if s.vessel.call_sign == call_sign.strip()),
        None,
    )


def voyage_from_sailing(sailing: TerminalSailing, voyage_id: str, transit_days: float) -> Voyage:
    vessel, snapshot = sailing.vessel, sailing.snapshot
    if vessel.cy_cutoff is None:
        raise ValueError(f"{vessel.vessel_name} has no published CY cutoff")
    return Voyage(
        id=voyage_id,
        vessel_name=vessel.vessel_name,
        terminal=snapshot.terminal.scenario_terminal,
        origin="KRPUS",
        destination="HKHKG",
        etd=vessel.etd,
        cy_cutoff=vessel.cy_cutoff,
        transit_days=transit_days,
        source=(
            f"{snapshot.terminal.code} 공개 선석 계획 {snapshot.fetched_at.astimezone(KST):%Y-%m-%d} 조회"
            f" ({vessel.call_sign}, {vessel.voyage}, 항해일은 가정) {snapshot.source_url}"
        ),
    )
