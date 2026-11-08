from pydantic import BaseModel

from freshswitch_backend.domain.models import Quantity

JLST = "JLST 6권 2호(2025.6) 부산→홍콩 딸기 실증"


def _q(value: float, unit: str, source: str) -> Quantity:
    return Quantity(value=value, unit=unit, source=source)


class ColdchainParams(BaseModel):
    reference_outage_minutes: Quantity = _q(
        356, "분", f"{JLST}: 딸기 노선 누적 단전 5시간 56분, 가장 긴 구간은 항만 터미널"
    )
    reference_no_outage_loss_rate: Quantity = _q(
        0.015,
        "비율",
        f"{JLST}: 단전 없음 손실률 1~2%(모의) 중앙값. 시나리오 딸기 기본 손실률과 같음",
    )
    reference_outage_loss_rate: Quantity = _q(
        0.07, "비율", f"{JLST}: 단전 있음(누적 5시간 56분) 손실률 6~8%(실측) 중앙값"
    )
    reference_max_loss_rate: Quantity = _q(
        0.08, "비율", f"{JLST}: 단전 있음 6~8% 상한. 시나리오 딸기 해상 손실 상한과 같음"
    )
    reference_excess_c: Quantity = _q(
        3.0,
        "°C",
        "가정: 기준 단전 동안 적정 온도 상한 위로 오른 평균 품온. 제안서 7-1 이탈 경고 2~4°C의 중앙",
    )
    outage_weight: Quantity = _q(
        0.5,
        "비율",
        "가정: 노출 지수에서 누적 단전 시간 몫. 나머지는 온도 이탈(°C·분) 몫. "
        "30°C 2시간 노출에도 손실이 측정된다(제안서 4-6)는 점을 온도 몫으로 반영",
    )
    warning_excursion_c: Quantity = _q(
        2.0, "°C", "제안서 7-1: 전원을 끊으면 2~4°C 이탈 경고. 적정 온도 범위 밖 2°C부터 경고"
    )
    critical_excursion_c: Quantity = _q(
        4.0, "°C", "제안서 7-1: 2~4°C 이탈 경고의 상단. 4°C 넘게 벗어나면 위험"
    )
    critical_continuous_minutes: Quantity = _q(
        120,
        "분",
        "가정: 연속 단전 2시간이면 위험. 30°C 2시간 노출에도 손실이 측정된다(제안서 4-6)",
    )
    plug_request_after_minutes: Quantity = _q(
        10, "분", "가정: 순간 정전·플러그 교체를 거르고 터미널 우선 연결을 요청하는 연속 단전 시간"
    )
    genset_after_minutes: Quantity = _q(
        30,
        "분",
        "가정: 연속 단전이 30분을 넘으면 이동형 전원(디젤 젯셋, 친환경 옵션 배터리 젯셋 "
        "Thermo King E-COOLPAC)을 배정. 제안서 4-2",
    )
    entry_adjust_after_minutes: Quantity = _q(
        60,
        "분",
        "가정: 누적 단전 1시간이면 반입·선적 시각을 플러그 확보 시각에 맞춰 다시 잡는다. 제안서 4-2",
    )
    cold_storage_cumulative_minutes: Quantity = _q(
        240,
        "분",
        "가정: 누적 단전 4시간(기준 5시간 56분의 약 2/3)이면 현지 냉장창고 예약 요청. 제안서 4-1",
    )
    cold_storage_loss_rate: Quantity = _q(
        0.06,
        "비율",
        f"{JLST}: 단전 있음 손실률 하한 6%. 예상 손실률이 이 값을 넘어도 냉장창고 예약",
    )
    insurance_continuous_minutes: Quantity = _q(
        480, "분", "GCMI 2025: 적하보험 냉동기 고장 특약은 냉각이 8시간 이상 연속으로 멈춰야 보상"
    )
    max_gap_minutes: Quantity = _q(
        60,
        "분",
        "가정: 측정 간격이 이보다 길면 센서 공백으로 보고 이 시간까지만 앞 상태를 이어 센다",
    )
    default_temp_min_c: Quantity = _q(
        0.0,
        "°C",
        "시나리오 딸기 CA 조건(원예원 품목별 CA 가이드라인). 계획이 없을 때 쓰는 적정 범위",
    )
    default_temp_max_c: Quantity = _q(
        2.0,
        "°C",
        "시나리오 딸기 CA 조건(원예원 품목별 CA 가이드라인). 계획이 없을 때 쓰는 적정 범위",
    )

    @property
    def reference_degree_minutes(self) -> float:
        return self.reference_outage_minutes.value * self.reference_excess_c.value

    @property
    def reference_fraction(self) -> float:
        base = self.reference_no_outage_loss_rate.value
        span = self.reference_max_loss_rate.value - base
        if span <= 0:
            return 1.0
        return (self.reference_outage_loss_rate.value - base) / span
