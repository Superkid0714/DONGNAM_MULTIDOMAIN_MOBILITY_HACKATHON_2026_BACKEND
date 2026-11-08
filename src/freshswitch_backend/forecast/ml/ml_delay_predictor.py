"""
FreshSwitch 선박 출항 지연 예측기 (ML)

- 학습: Colab, 해양수산부 선박입출항정보(VsslEtrynd5/Info5, 부산 020) 컨테이너선 49,628항차 (2023.1~2026.11)
- 정답: 실제 출항 시각 - 입항 시 신고한 출항 예정 시각 (시간)
- 모델: p50 = LightGBM p50 + CatBoost p50 평균, p90 = max(p50, LightGBM p90) + 보정값 (+ 강풍 규칙)
- 두 가지 모드
    ops  : 입항 후 (입항 신고 정보 사용, 31개 피처)
    plan : 입항 전 / 출항 3일 전 (선사·항로·이력만, 16개 피처)

배치 위치 예: src/freshswitch_backend/forecast/ml/
    delay_model_meta.json, delay_{ops,plan}_p50.txt, delay_{ops,plan}_p90.txt, delay_{ops,plan}_cb_p50.cbm

필요 패키지: lightgbm, catboost, pandas, numpy   (uv add lightgbm catboost)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
    from catboost import CatBoostRegressor
except ImportError as e:  # 패키지 없으면 기존 통계 예측기로 대체하도록 호출부에서 처리
    raise ImportError("lightgbm, catboost 가 필요합니다: uv add lightgbm catboost") from e


# 단기예보 WSD(10분 평균풍속)만 있을 때 순간최대풍속 추정 배수 (가정값, 운영 데이터로 재보정 권장)
GUST_FACTOR_FROM_WSD = 1.5
HISTORY_COLS = ["clsgn", "etryptDt", "tkoffDt", "delay_h", "satmntEntrpsNm", "terminal"]


@dataclass
class VoyageInput:
    """예측할 항차 한 건. 필드명은 해수부 API 필드명과 동일."""
    clsgn: str                                  # 호출부호
    tkoffPrrrnDt: pd.Timestamp                  # 신고(또는 선사 스케줄) 출항 예정 시각, KST
    etryptDt: Optional[pd.Timestamp] = None     # 실제 입항 시각 (ops 필수, plan 은 예정 입항 시각 또는 None)
    laidupFcltyNm: Optional[str] = None         # 예: "신항 7부두 1선석"
    satmntEntrpsNm: Optional[str] = None        # 대리점
    vsslNltyNm: Optional[str] = None            # 선박 국적
    prvsDpmprtPrtNm: Optional[str] = None       # 직전 항구
    nxlnptPrtNm: Optional[str] = None           # 다음 항구
    nxlnptNatPrtCd: Optional[str] = None        # 다음 항구 코드 (HK.. 이면 홍콩행)
    grtg: Optional[float] = None                # 총톤수
    ldadngTon: Optional[float] = None           # 적재 톤수 (입항 신고)
    landngFrghtTon: Optional[float] = None      # 양하 톤수 (입항 신고)
    trnpdtTon: Optional[float] = None           # 환적 톤수 (입항 신고)
    tugYn: Optional[str] = None
    piltgYn: Optional[str] = None
    forecast_gust_ms: Optional[float] = None    # 출항 예정일 순간최대풍속 예보 (없으면 forecast_wsd_ms 사용)
    forecast_wsd_ms: Optional[float] = None     # 단기예보 WSD


@dataclass
class DelayPrediction:
    mode: str
    p50_h: float
    p90_h: float
    eta_departure_p50: pd.Timestamp
    eta_departure_p90: pd.Timestamp
    strong_wind_rule: bool
    source: str = "model"
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "delay_p50_h": round(self.p50_h, 2),
            "delay_p90_h": round(self.p90_h, 2),
            "departure_p50": self.eta_departure_p50.isoformat(),
            "departure_p90": self.eta_departure_p90.isoformat(),
            "strong_wind_rule": self.strong_wind_rule,
            "source": self.source,
            "assumptions": self.assumptions,
        }


def terminal_of(fclty: Optional[str]) -> str:
    if not fclty:
        return "기타"
    m = re.match(r"^(.*?부두)", fclty)
    return m.group(1) if m else "기타"


class MLDelayPredictor:
    def __init__(self, model_dir: str | Path):
        d = Path(model_dir)
        self.meta = json.loads((d / "delay_model_meta.json").read_text(encoding="utf-8"))
        self.categories: dict[str, list[str]] = self.meta["categories"]
        self.cat_cols: list[str] = self.meta["categorical"]
        self.features = {"ops": self.meta["ops_features"], "plan": self.meta["plan_features"]}
        self.q90_adj = float(self.meta.get("q90_adj_h", 0.0))
        wr = self.meta.get("weather_rules", {}).get("strong_wind", {})
        self.wind_add = float(wr.get("p90_add_h", 0.0))
        self.wind_threshold = 20.0
        self.models = {}
        for mode in ("ops", "plan"):
            cb = CatBoostRegressor()
            cb.load_model(str(d / f"delay_{mode}_cb_p50.cbm"))
            self.models[mode] = {
                "lgb50": lgb.Booster(model_file=str(d / f"delay_{mode}_p50.txt")),
                "lgb90": lgb.Booster(model_file=str(d / f"delay_{mode}_p90.txt")),
                "cb50": cb,
            }

    # ---------- 피처 계산 (Colab 학습 코드와 동일한 정의) ----------
    @staticmethod
    def _prep_history(history: pd.DataFrame) -> pd.DataFrame:
        """history: 부산항 컨테이너선 과거 항차. 필요한 컬럼 = HISTORY_COLS
        (tkoffDt 가 없으면 아직 출항 전 = 항내 체류 중으로 취급)"""
        h = history.copy()
        for c in ("etryptDt", "tkoffDt"):
            h[c] = pd.to_datetime(h[c])
        if "terminal" not in h and "laidupFcltyNm" in h:
            h["terminal"] = h["laidupFcltyNm"].map(terminal_of)
        return h

    @staticmethod
    def _mean_known(h: pd.DataFrame, t: pd.Timestamp, mask=None, hours: Optional[int] = None):
        """t 시점 이전에 이미 출항해서 지연이 확정된 항차들의 평균 지연과 건수"""
        sel = h["tkoffDt"].notna() & (h["tkoffDt"] < t)
        if hours is not None:
            sel &= h["tkoffDt"] >= t - pd.Timedelta(hours=hours)
        if mask is not None:
            sel &= mask
        sub = h.loc[sel, "delay_h"]
        return (float(sub.mean()) if len(sub) else np.nan), int(len(sub))

    def build_features(self, v: VoyageInput, history: pd.DataFrame, mode: str,
                       now: Optional[pd.Timestamp] = None) -> pd.DataFrame:
        h = self._prep_history(history)
        sched = pd.Timestamp(v.tkoffPrrrnDt)
        # 예측 기준 시각: ops = 실제 입항, plan = 지금(없으면 예정 입항/출항 3일 전)
        if mode == "ops":
            if v.etryptDt is None:
                raise ValueError("ops 모드는 etryptDt(실제 입항 시각)가 필요합니다")
            t = pd.Timestamp(v.etryptDt)
        else:
            t = pd.Timestamp(now) if now is not None else (
                pd.Timestamp(v.etryptDt) if v.etryptDt is not None else sched - pd.Timedelta(days=3))
        arr = pd.Timestamp(v.etryptDt) if v.etryptDt is not None else t
        term = terminal_of(v.laidupFcltyNm)
        h = h[h["etryptDt"] < t]

        cargo_in = (v.landngFrghtTon or 0.0) + (v.trnpdtTon or 0.0)
        plan_dwell = (sched - arr).total_seconds() / 3600 if v.etryptDt is not None else np.nan
        row = {
            "grtg": v.grtg, "ldadngTon": v.ldadngTon, "landngFrghtTon": v.landngFrghtTon,
            "trnpdtTon": v.trnpdtTon, "cargo_in": cargo_in,
            "plan_dwell_h": plan_dwell,
            "cargo_per_planh": cargo_in / max(plan_dwell, 1.0) if not np.isnan(plan_dwell) else np.nan,
            "year": arr.year, "month": arr.month, "weekday": arr.weekday(), "hour": arr.hour,
            "sched_hour": sched.hour,
            "is_newport": int("신항" in (v.laidupFcltyNm or "")),
            "is_hk": int(str(v.nxlnptNatPrtCd or "").startswith("HK")),
            "tug": int(v.tugYn == "Y"), "pilot": int(v.piltgYn == "Y"),
            "vsl_visits": int((h["clsgn"] == v.clsgn).sum()),
        }
        row["vsl_past_delay"], _ = self._mean_known(h, t, h["clsgn"] == v.clsgn)
        row["agent_past_delay"], _ = self._mean_known(h, t, h["satmntEntrpsNm"] == v.satmntEntrpsNm)
        row["term_past_delay"], _ = self._mean_known(h, t, h["terminal"] == term)
        row["term_delay_24h"], row["term_dep_24h"] = self._mean_known(h, t, h["terminal"] == term, 24)
        row["port_delay_24h"], row["port_dep_24h"] = self._mean_known(h, t, None, 24)
        in_port = h["tkoffDt"].isna() | (h["tkoffDt"] >= t)
        row["in_port"] = int(in_port.sum())
        row["in_port_term"] = int((in_port & (h["terminal"] == term)).sum())

        cats = {"terminal": term, "satmntEntrpsNm": v.satmntEntrpsNm, "vsslNltyNm": v.vsslNltyNm,
                "prvsDpmprtPrtNm": v.prvsDpmprtPrtNm, "nxlnptPrtNm": v.nxlnptPrtNm}
        for c, val in cats.items():
            allowed = self.categories[c]
            val = str(val) if val is not None else "nan"
            row[c] = val if val in allowed else ("기타" if "기타" in allowed else np.nan)

        X = pd.DataFrame([row])[self.features[mode]]
        for c in self.cat_cols:
            if c in X:
                X[c] = pd.Categorical(X[c], categories=self.categories[c])
        return X

    # ---------- 예측 ----------
    def predict(self, v: VoyageInput, history: pd.DataFrame, mode: str = "ops",
                now: Optional[pd.Timestamp] = None) -> DelayPrediction:
        X = self.build_features(v, history, mode, now)
        m = self.models[mode]
        lgb50 = float(m["lgb50"].predict(X)[0])
        Xcb = X.copy()
        for c in self.cat_cols:
            if c in Xcb:
                Xcb[c] = Xcb[c].astype(str)
        cb50 = float(m["cb50"].predict(Xcb)[0])
        p50 = (lgb50 + cb50) / 2
        p90 = max(p50, float(m["lgb90"].predict(X)[0])) + self.q90_adj

        assumptions = [f"p90 보정 +{self.q90_adj:.2f}h"]
        gust = v.forecast_gust_ms
        if gust is None and v.forecast_wsd_ms is not None:
            gust = v.forecast_wsd_ms * GUST_FACTOR_FROM_WSD
            assumptions.append(f"순간풍속 = 단기예보 WSD x {GUST_FACTOR_FROM_WSD} (가정)")
        strong = gust is not None and gust >= self.wind_threshold
        if strong:
            p90 += self.wind_add
            assumptions.append(f"강풍 규칙 적용 (+{self.wind_add:.1f}h)")
        if mode == "plan":
            assumptions.append("계획용: 입항 신고 정보 없이 선사·항로·이력으로 추정")

        sched = pd.Timestamp(v.tkoffPrrrnDt)
        return DelayPrediction(
            mode=mode, p50_h=p50, p90_h=p90,
            eta_departure_p50=sched + pd.Timedelta(hours=p50),
            eta_departure_p90=sched + pd.Timedelta(hours=p90),
            strong_wind_rule=strong, assumptions=assumptions,
        )


if __name__ == "__main__":
    # 사용 예 (WAN HAI 612, 2025-11-01 입항)
    import sys
    pred = MLDelayPredictor(sys.argv[1] if len(sys.argv) > 1 else "models")
    hist = pd.read_csv(sys.argv[2]) if len(sys.argv) > 2 else pd.DataFrame(columns=HISTORY_COLS)
    if "terminal" not in hist and "laidupFcltyNm" in hist:
        hist["terminal"] = hist["laidupFcltyNm"].map(terminal_of)
    v = VoyageInput(
        clsgn="5LOE3", etryptDt=pd.Timestamp("2025-11-01 00:05"), tkoffPrrrnDt=pd.Timestamp("2025-11-01 14:00"),
        laidupFcltyNm="신항 7부두 1선석", satmntEntrpsNm="만해항운한국(주)", vsslNltyNm="라이베리아",
        prvsDpmprtPrtNm="SAN ANTONIO", nxlnptPrtNm="QINGDAO", nxlnptNatPrtCd="CNTAO",
        grtg=68687, ldadngTon=19888, landngFrghtTon=1286, trnpdtTon=417, tugYn="Y", piltgYn="Y",
        forecast_wsd_ms=6.0,
    )
    print(json.dumps(pred.predict(v, hist, "ops").to_dict(), ensure_ascii=False, indent=2))
    print(json.dumps(pred.predict(v, hist, "plan").to_dict(), ensure_ascii=False, indent=2))
