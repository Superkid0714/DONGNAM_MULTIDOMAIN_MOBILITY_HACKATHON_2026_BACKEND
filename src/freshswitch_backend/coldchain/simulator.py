import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from freshswitch_backend.coldchain.models import (
    HandoverReadingIn,
    HandoverStep,
    PresetInfo,
    ReadingIn,
    SimulationPreset,
)
from freshswitch_backend.coldchain.params import ColdchainParams
from freshswitch_backend.domain.models import KST

DEFAULT_START = datetime(2026, 11, 17, 9, 0, tzinfo=KST)
AMBIENT_C = 15.0
WARMING_TAU_MIN = 240.0
COOLING_TAU_MIN = 45.0


@dataclass(frozen=True)
class Phase:
    minutes: int
    power_on: bool
    handover_step: int | None = None


@dataclass(frozen=True)
class Preset:
    id: SimulationPreset
    title: str
    description: str
    phases: tuple[Phase, ...]
    step_minutes: int = 5
    cooling_tau_minutes: float = COOLING_TAU_MIN

    @property
    def outage_minutes(self) -> int:
        return sum(p.minutes for p in self.phases if not p.power_on)

    @property
    def total_minutes(self) -> int:
        return sum(p.minutes for p in self.phases)

    @property
    def readings(self) -> int:
        return self.total_minutes // self.step_minutes + 1

    def info(self) -> PresetInfo:
        return PresetInfo(
            id=self.id,
            title=self.title,
            description=self.description,
            step_minutes=self.step_minutes,
            readings=self.readings,
            outage_minutes=self.outage_minutes,
        )


PRESETS: dict[SimulationPreset, Preset] = {
    "power_cut_restore": Preset(
        id="power_cut_restore",
        title="단전 복구 (4시간 단전)",
        description="정상 1시간 → 터미널 단전 4시간(품온 상승) → 전원 복구 후 2시간 재냉각. "
        "경고·위험, 우선 플러그·젯셋·반입 조정·현지 냉장창고 조치가 모두 뜬다",
        phases=(Phase(60, True), Phase(240, False), Phase(120, True)),
    ),
    "short_outage": Preset(
        id="short_outage",
        title="짧은 단전 (45분)",
        description="정상 1시간 → 단전 45분 → 복구 1시간. 경고와 우선 플러그·젯셋 조치까지만 뜬다",
        phases=(Phase(60, True), Phase(45, False), Phase(60, True)),
    ),
    "normal": Preset(
        id="normal",
        title="정상 운송 (2시간)",
        description="전원이 계속 켜져 있고 품온이 적정 범위 안. 경고·조치 없음",
        phases=(Phase(120, True),),
    ),
    "sea_to_land": Preset(
        id="sea_to_land",
        cooling_tau_minutes=15.0,
        title="해상→육상 전환 (계획 단전 45분)",
        description="선상 운항 → 양하 계획 단전 → 야드 대기 → 혼잡으로 연결 지연 → 우선 연결로 복전 "
        "→ 트럭 → 창고 입고. 허용 시간을 넘긴 단전분만 손실에 반영한다",
        phases=(
            Phase(30, True, 1),
            Phase(10, False, 2),
            Phase(10, False, 3),
            Phase(25, False, 4),
            Phase(15, True, 5),
            Phase(30, True, 6),
            Phase(30, True, 7),
        ),
    ),
}


def handover_steps(params: ColdchainParams) -> list[HandoverStep]:
    warning = f"{params.planned_outage_warning_minutes.value:g}"
    allowance = f"{params.planned_outage_allowance_minutes.value:g}"
    return [
        HandoverStep(step=1, title="선상 운항", power="ON", system="온도와 CA 조건 정상"),
        HandoverStep(
            step=2,
            title="양하 시작",
            power="OFF (계획됨)",
            system=f"알림 없이 계획 단전 타이머 시작, 허용 {allowance}분",
        ),
        HandoverStep(step=3, title="야드 도착", power="ON 예정", system="터미널에 전원 연결 대기"),
        HandoverStep(
            step=4,
            title="야드 혼잡으로 연결 지연",
            power="OFF 계속",
            system=f"{warning}분에 주의 알림, {allowance}분 초과 시 이상 단전으로 전환",
        ),
        HandoverStep(
            step=5, title="조치", power="ON", system="포워더가 터미널에 우선 연결 요청, 복전 확인"
        ),
        HandoverStep(
            step=6,
            title="트럭 상차·이동",
            power="트럭 전원 ON",
            system="단전 누적 시간과 온도 계속 추적",
        ),
        HandoverStep(
            step=7, title="창고 입고·검수", power="ON", system="초과 단전분만큼 예상 손실률 반영"
        ),
    ]


def preset_readings(
    preset: Preset,
    *,
    device_id: str,
    container_id: str,
    plan_id: str | None,
    band_min_c: float,
    band_max_c: float,
    start: datetime | None = None,
) -> list[ReadingIn]:
    start = start or DEFAULT_START
    setpoint = (band_min_c + band_max_c) / 2
    step = preset.step_minutes
    schedule: list[tuple[bool, int | None]] = []
    for phase in preset.phases:
        schedule.extend([(phase.power_on, phase.handover_step)] * (phase.minutes // step))
    schedule.append(schedule[-1] if schedule else (True, None))
    readings: list[ReadingIn] = []
    temp = setpoint
    for i, (power_on, handover_step) in enumerate(schedule):
        if i > 0:
            if schedule[i - 1][0]:
                temp = setpoint + (temp - setpoint) * math.exp(-step / preset.cooling_tau_minutes)
            else:
                temp = AMBIENT_C + (temp - AMBIENT_C) * math.exp(-step / WARMING_TAU_MIN)
        wiggle = 0.1 * math.sin(i / 3)
        fields = {
            "device_id": device_id,
            "container_id": container_id,
            "plan_id": plan_id,
            "ts": start + timedelta(minutes=i * step),
            "temp_c": round(temp + wiggle, 2),
            "power_on": power_on,
            "humidity_pct": round(92 - 0.5 * max(0.0, temp - setpoint), 1),
            "door_open": False,
        }
        readings.append(
            ReadingIn(**fields)
            if handover_step is None
            else HandoverReadingIn(**fields, handover_step=handover_step)
        )
    return readings
