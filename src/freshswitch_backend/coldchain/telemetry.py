import hashlib
import math
import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from freshswitch_backend.coldchain.devices import token_hash
from freshswitch_backend.coldchain.models import ActionHandler, BindingRequest, ReadingIn, Stage
from freshswitch_backend.coldchain.simulator import AMBIENT_C, COOLING_TAU_MIN, WARMING_TAU_MIN
from freshswitch_backend.coldchain.store import ColdchainStore
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import BindingRow, DeviceRow, OrganizationRow

DEVICE_PREFIX = "RCM-"
DEVICE_TAC = "35"
STEP_MINUTES = 5
DOOR_TAU_MIN = 240.0
NOISE_SD_C = 0.06
NOISE_MAX_C = 0.15
DONE_NOTE = "육상 전원 연결 확인 · 품온 적정 범위 복귀"


def _rng(seed: str) -> random.Random:
    return random.Random(int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big"))


def _luhn_digit(body: str) -> int:
    total = 0
    for index, char in enumerate(reversed(body)):
        digit = int(char)
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - total % 10) % 10


def luhn_valid(number: str) -> bool:
    return number.isdigit() and _luhn_digit(number[:-1]) == int(number[-1])


def reefer_device_id(seed: str) -> str:
    rng = _rng(f"device:{seed}")
    body = DEVICE_TAC + "".join(str(rng.randrange(10)) for _ in range(12))
    return f"{DEVICE_PREFIX}{body}{_luhn_digit(body)}"


def reefer_device_name(container_id: str) -> str:
    return f"리퍼 원격 모니터링 · {container_id}"


def setpoint_for(band_min_c: float, band_max_c: float) -> float:
    width = max(0.0, band_max_c - band_min_c)
    return round(band_min_c + min(0.5, 0.4 * width), 1)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def covers(self, ts: datetime) -> bool:
        return self.start <= ts < self.end

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class Defrost:
    window: Window
    amplitude_c: float


@dataclass(frozen=True)
class ReeferTrace:
    readings: list[ReadingIn]
    setpoint_c: float
    door: Window
    outage: Window
    defrosts: list[Defrost]

    @property
    def start(self) -> datetime:
        return self.readings[0].ts

    @property
    def end(self) -> datetime:
        return self.readings[-1].ts


def _aligned(moment: datetime, step: int) -> datetime:
    floored = moment.replace(second=0, microsecond=0)
    return floored - timedelta(minutes=floored.minute % step)


def _minutes(rng: random.Random, low: int, high: int, step: int) -> int:
    return rng.randrange(low // step, high // step + 1) * step


def _defrosts(
    rng: random.Random,
    start: datetime,
    end: datetime,
    blocked: list[Window],
    step: int,
    headroom_c: float,
) -> list[Defrost]:
    out: list[Defrost] = []
    at = start + timedelta(minutes=_minutes(rng, 150, 300, step))
    while True:
        window = Window(at, at + timedelta(minutes=_minutes(rng, 20, 30, step)))
        if window.end + timedelta(minutes=30) > end:
            return out
        margin = timedelta(minutes=60)
        if not any(
            window.start < b.end + margin and b.start - margin < window.end for b in blocked
        ):
            out.append(Defrost(window, round(rng.uniform(0.4, 0.8) * headroom_c, 2)))
        at = window.start + timedelta(minutes=_minutes(rng, 360, 480, step))


def _bump(ts: datetime, defrosts: list[Defrost]) -> tuple[float, bool]:
    for item in defrosts:
        if item.window.covers(ts):
            progress = (ts - item.window.start).total_seconds() / 60 / item.window.minutes
            return item.amplitude_c * math.sin(math.pi * progress), True
    return 0.0, False


def reefer_trace(
    seed: str,
    *,
    device_id: str,
    container_id: str,
    plan_id: str | None,
    band_min_c: float,
    band_max_c: float,
    end: datetime,
    step_minutes: int = STEP_MINUTES,
) -> ReeferTrace:
    rng = _rng(f"trace:{seed}")
    step = step_minutes
    last = _aligned(end, step)
    count = _minutes(rng, 26 * 60, 34 * 60, step) // step
    start = last - timedelta(minutes=count * step)
    setpoint = setpoint_for(band_min_c, band_max_c)
    door = Window(start, start + timedelta(minutes=_minutes(rng, 10, 15, step)))
    cut = start + timedelta(minutes=_minutes(rng, 180, 360, step))
    outage = Window(cut, cut + timedelta(minutes=_minutes(rng, 20, 30, step)))
    defrosts = _defrosts(
        rng,
        start,
        last,
        [Window(start, door.end + timedelta(minutes=90)), outage],
        step,
        max(0.0, band_max_c - setpoint - NOISE_MAX_C),
    )
    temp = band_max_c + rng.uniform(0.1, 0.3)
    readings: list[ReadingIn] = []
    for index in range(count + 1):
        ts = start + timedelta(minutes=index * step)
        power_on = not outage.covers(ts)
        door_open = door.covers(ts)
        if index > 0:
            if not power_on:
                temp = AMBIENT_C + (temp - AMBIENT_C) * math.exp(-step / WARMING_TAU_MIN)
            elif door_open:
                temp = AMBIENT_C + (temp - AMBIENT_C) * math.exp(-step / DOOR_TAU_MIN)
            else:
                temp = setpoint + (temp - setpoint) * math.exp(-step / COOLING_TAU_MIN)
        bump, defrosting = _bump(ts, defrosts)
        noise = max(-NOISE_MAX_C, min(NOISE_MAX_C, rng.gauss(0, NOISE_SD_C)))
        humidity = 92.0 + rng.gauss(0, 0.8)
        if door_open:
            humidity = rng.uniform(78, 84)
        elif not power_on:
            humidity += 1.5
        elif defrosting:
            humidity -= 2.0
        readings.append(
            ReadingIn(
                device_id=device_id,
                container_id=container_id,
                plan_id=plan_id,
                ts=ts,
                temp_c=round(temp + bump + noise, 1),
                power_on=power_on,
                humidity_pct=round(max(0.0, min(100.0, humidity)), 1),
                door_open=door_open,
            )
        )
    return ReeferTrace(readings, setpoint, door, outage, defrosts)


@dataclass(frozen=True)
class SeededContainer:
    container_id: str
    device_id: str
    readings: int
    trace: ReeferTrace


def seed_container(
    store: ColdchainStore,
    *,
    org_id: str,
    plan_id: str,
    container_id: str,
    seed: str,
    end: datetime,
    stage: Stage = "terminal",
) -> SeededContainer | None:
    device_id = reefer_device_id(seed)
    state = store.state(container_id, plan_id, 0, org_id)
    band = state.excursion
    trace = reefer_trace(
        seed,
        device_id=device_id,
        container_id=container_id,
        plan_id=plan_id,
        band_min_c=band.band_min_c,
        band_max_c=band.band_max_c,
        end=end,
    )
    with database.begin() as db:
        if db.get(DeviceRow, device_id) is not None or db.get(BindingRow, device_id) is not None:
            return None
        org_type = db.scalar(select(OrganizationRow.type).where(OrganizationRow.id == org_id))
        db.add(
            DeviceRow(
                device_id=device_id,
                org_id=org_id,
                token_hash=token_hash(secrets.token_urlsafe(32)),
                name=reefer_device_name(container_id),
                created_at=trace.start,
                last_seen_at=trace.end,
            )
        )
    binding = store.bind(
        BindingRequest(
            device_id=device_id, container_id=container_id, plan_id=plan_id, stage=stage
        ),
        org_id,
    )
    with database.begin() as db:
        row = db.get(BindingRow, device_id)
        if row is not None:
            row.payload = binding.model_copy(update={"bound_at": trace.start}).model_dump(
                mode="json"
            )
    result = store.ingest(trace.readings, org_id)
    if result.rejected:
        raise ValueError(f"측정값이 거부되었습니다: {result.rejected[0].reason}")
    handler = ActionHandler(
        org_type="forwarder" if org_type == "forwarder" else "platform", org_id=org_id
    )
    for action in store.state(container_id, plan_id, 0, org_id).actions:
        if action.status != "done":
            store.update_action(
                container_id, action.key, "complete", handler, DONE_NOTE, plan_id, org_id
            )
    return SeededContainer(container_id, device_id, result.accepted, trace)
