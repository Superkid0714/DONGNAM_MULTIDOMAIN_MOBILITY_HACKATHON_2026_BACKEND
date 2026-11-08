# 아두이노 단전 복구 데모

소형 냉각 박스 안의 온도와 박스 전원 상태를 아두이노가 2초마다 JSON 한 줄로 시리얼에 찍고,
`bridge.py`가 그 줄을 `POST /api/coldchain/readings`로 보낸다. 손실 디지털 트윈 화면은
`GET /api/coldchain/containers/{container_id}`와 `GET /api/coldchain/alerts?since=`를 폴링한다.

## 준비물

| 부품 | 연결 |
| --- | --- |
| 아두이노 우노(또는 나노) | USB로 노트북 |
| DS18B20 방수형 온도센서 | 데이터 → D2, 4.7kΩ 풀업(데이터–5V), VCC 5V, GND |
| 냉각 박스 전원 감지 | 박스 어댑터 12V → 분압(10kΩ/4.7kΩ, 약 3.8V) → D3. 공통 GND. 어댑터를 뽑으면 LOW = 단전 |
| 문 스위치(선택) | 리드 스위치 D4 ↔ GND (내부 풀업, 열리면 HIGH) |

전원 감지 회로가 없으면 D3에 점퍼선을 5V(전원 켜짐)·GND(단전)로 옮겨 꽂아도 된다.

## 스케치 올리기

1. 아두이노 IDE 라이브러리 관리자에서 `OneWire`, `DallasTemperature` 설치
2. `freshswitch_sensor/freshswitch_sensor.ino` 열고 `DEVICE_ID`를 필요하면 바꾼 뒤 업로드
3. 시리얼 모니터(9600)에 아래처럼 찍히면 정상

```
{"device_id":"arduino-01","temp_c":1.94,"power_on":true,"door_open":false,"uptime_ms":12034}
```

센서가 빠지면 `temp_c`가 `null`로 나오고 브리지는 그 줄을 건너뛴다.

## 브리지 실행

pyserial은 본 프로젝트 의존성에 넣지 않았다. 실행할 때만 붙인다.

```bash
# 1) 계획 만들고 장치를 컨테이너에 연결 (손실액 계산에 계획이 필요)
PLAN=$(curl -s -X POST localhost:8000/api/plans -H 'content-type: application/json' \
  -d '{"scenario_id":"default"}' | jq -r .plan_id)
curl -s -X POST localhost:8000/api/coldchain/bindings -H 'content-type: application/json' \
  -d "{\"device_id\":\"arduino-01\",\"container_id\":\"CA-01\",\"plan_id\":\"$PLAN\",\"stage\":\"terminal\"}"

# 2) 시리얼 → API
uv run --with pyserial python scripts/arduino/bridge.py --port /dev/ttyACM0 \
  --container CA-01 --plan-id "$PLAN"
```

| 옵션 | 기본값 | 뜻 |
| --- | --- | --- |
| `--port` | | 시리얼 포트 (`/dev/ttyACM0`, macOS `/dev/cu.usbmodem*`, 윈도 `COM3`) |
| `--stdin` | | 표준 입력의 JSON 줄을 보낸다(파일 재생·테스트용) |
| `--fake` | | 아두이노 없이 가짜 측정값. Enter를 누를 때마다 전원 토글 |
| `--api` | `http://127.0.0.1:8000/api` | API 주소 (docker compose면 `http://127.0.0.1:18103/api`) |
| `--container` | `CA-01` | 계획의 컨테이너 ID |
| `--plan-id` | 없음 | 계획 ID. 바인딩으로 이미 연결했으면 생략 가능 |
| `--time-scale` | `60` | 시각 가속. 60이면 실제 1초 = 1분, 4분 전원을 뽑으면 4시간 단전으로 계산 |
| `--interval` | `2` | `--fake` 측정 간격(초) |
| `--dry-run` | | 보내지 않고 변환된 요청 본문만 출력 |

## 무대 순서 (`--time-scale 60` 기준 약 5분, 120이면 절반)

1. 브리지 실행, 박스 전원 켜진 상태로 30초 → 트윈 `normal`, 센서 손실률 1.5%
2. 어댑터를 뽑는다 → 즉시 `전원 차단`·`경고` 알림, 10초(=10분) 뒤 터미널 우선 플러그 요청, 30초 뒤 젯셋 배정, 1분 뒤 반입 시간 조정
3. 박스 안 온도가 2℃ 넘게 벗어나면 경고, 4℃ 넘게 벗어나거나 2분(=2시간) 연속 단전이면 위험
4. 4분(=4시간) 뒤 현지 냉장창고 예약 요청이 뜬다. 예상 손실률은 단전 시간만으로 약 3.4%, 박스 온도가 평균 3℃ 넘게 벗어나 있었다면 6~7%까지 오른다
5. 어댑터를 다시 꽂는다 → `전원 복구`, 온도가 돌아오면 `경고 해제`. 손실률은 누적이라 내려가지 않는다

실물 박스는 온도가 천천히 오르므로 손실률은 단전 시간 몫이 먼저 오르고 온도 몫은 늦게 붙는다.
하드웨어 없이 같은 장면을 보려면 `POST /api/coldchain/simulate`(`interval_seconds: 0.5`)나
`bridge.py --fake`를 쓴다.
