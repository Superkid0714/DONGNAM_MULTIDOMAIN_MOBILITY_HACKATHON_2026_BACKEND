#!/usr/bin/env python3
import argparse
import json
import math
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="아두이노 시리얼 JSON 줄 → FreshSwitch 수집 API")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--port", help="시리얼 포트 (예: /dev/ttyACM0, COM3). pyserial 필요")
    source.add_argument("--stdin", action="store_true", help="표준 입력의 JSON 줄을 그대로 보낸다")
    source.add_argument(
        "--fake", action="store_true", help="센서 없이 가짜 측정값. Enter로 전원 토글"
    )
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--api", default="http://127.0.0.1:8000/api", help="API 기본 주소")
    parser.add_argument("--container", default="CA-01", help="계획의 컨테이너 ID")
    parser.add_argument("--plan-id", default=None, help="계획 ID (손실액 계산용)")
    parser.add_argument("--device-id", default=None, help="장치 ID 덮어쓰기")
    parser.add_argument("--stage", default=None, choices=["trucking", "gate", "terminal", "vessel"])
    parser.add_argument(
        "--time-scale",
        type=float,
        default=60.0,
        help="시각 가속 배율. 60이면 실제 1초가 1분 (전원 4분 차단 = 4시간 단전)",
    )
    parser.add_argument("--interval", type=float, default=2.0, help="--fake 측정 간격(초)")
    parser.add_argument("--dry-run", action="store_true", help="보내지 않고 출력만")
    return parser.parse_args()


class Clock:
    def __init__(self, scale: float) -> None:
        self.scale = scale
        self.wall_start = time.monotonic()
        self.sim_start = datetime.now(KST)

    def now(self) -> datetime:
        elapsed = (time.monotonic() - self.wall_start) * self.scale
        return self.sim_start + timedelta(seconds=elapsed)


def post(api: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{api.rstrip('/')}/coldchain/readings",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def to_payload(raw: dict, args: argparse.Namespace, clock: Clock) -> dict | None:
    if raw.get("temp_c") is None or "power_on" not in raw:
        return None
    payload = {
        "device_id": args.device_id or raw.get("device_id") or "arduino-01",
        "container_id": args.container,
        "ts": clock.now().isoformat(),
        "temp_c": float(raw["temp_c"]),
        "power_on": bool(raw["power_on"]),
    }
    for key in ("humidity_pct", "door_open"):
        if raw.get(key) is not None:
            payload[key] = raw[key]
    if args.plan_id:
        payload["plan_id"] = args.plan_id
    if args.stage:
        payload["stage"] = args.stage
    return payload


def serial_lines(port: str, baud: int):
    try:
        import serial
    except ImportError:
        sys.exit(
            "pyserial이 필요합니다: uv run --with pyserial python scripts/arduino/bridge.py ..."
        )
    with serial.Serial(port, baud, timeout=5) as conn:
        while True:
            line = conn.readline().decode(errors="ignore").strip()
            if line:
                yield line


def fake_lines(interval: float, scale: float):
    state = {"power_on": True}

    def toggle() -> None:
        for _ in sys.stdin:
            state["power_on"] = not state["power_on"]
            print(f"전원 {'켜짐' if state['power_on'] else '꺼짐'}", file=sys.stderr)

    threading.Thread(target=toggle, daemon=True).start()
    temp = 1.0
    step_min = interval * scale / 60
    while True:
        target, tau = (1.0, 45.0) if state["power_on"] else (15.0, 240.0)
        temp = target + (temp - target) * math.exp(-step_min / tau)
        yield json.dumps({"device_id": "fake-01", "temp_c": round(temp, 2), **state})
        time.sleep(interval)


def main() -> None:
    args = parse_args()
    clock = Clock(args.time_scale)
    if args.port:
        lines = serial_lines(args.port, args.baud)
    elif args.fake:
        print("Enter를 누를 때마다 전원이 토글됩니다", file=sys.stderr)
        lines = fake_lines(args.interval, args.time_scale)
    else:
        lines = (line.strip() for line in sys.stdin if line.strip())
    for line in lines:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = to_payload(raw, args, clock)
        if payload is None:
            continue
        if args.dry_run:
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            continue
        try:
            result = post(args.api, payload)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"전송 실패: {exc}", file=sys.stderr)
            continue
        summary = result["containers"][0] if result.get("containers") else {}
        print(
            f"{payload['ts'][11:16]} {payload['temp_c']:5.2f}°C "
            f"전원 {'O' if payload['power_on'] else 'X'} "
            f"상태 {summary.get('status')} 손실률 {summary.get('sensor_loss_rate')}",
            flush=True,
        )
        for alert in result.get("new_alerts", []):
            print(f"  [{alert['level']}] {alert['title']}: {alert['message']}", flush=True)


if __name__ == "__main__":
    main()
