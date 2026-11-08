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
| `--container` | `CA-01` | 계획의 컨테이너 ID. `--device-token`을 주면 기본값 없이 바인딩이 정한다 |
| `--plan-id` | 없음 | 계획 ID. 바인딩으로 이미 연결했으면 생략 가능 |
| `--time-scale` | `60` | 시각 가속. 60이면 실제 1초 = 1분, 4분 전원을 뽑으면 4시간 단전으로 계산 |
| `--interval` | `2` | `--fake` 측정 간격(초) |
| `--device-token` | env `FRESHSWITCH_DEVICE_TOKEN` | SaaS 등록 기기 토큰. `X-Device-Token` 헤더로 보낸다 |
| `--dry-run` | | 보내지 않고 변환된 요청 본문만 출력 |

## SaaS(`/test`)로 보내기

SaaS 스택은 로그인 없는 판독을 받지 않는다. 조직에 등록한 기기의 토큰을 `X-Device-Token`으로
보내야 하고, 어느 컨테이너·계획에 들어갈지는 서버 바인딩이 정한다.

1. 계획을 만든 플랫폼 조직으로 로그인해 기기를 등록한다. 계획·바인딩·판독이 모두 같은 조직에 있어야
   하므로 등록과 바인딩은 계획 소유 조직(플랫폼) 세션으로만 한다. 포워더 세션은 플랫폼 계획에 바인딩할
   수 없다(404). 응답의 `device_token`은 이때 한 번만 나온다.
   데모(sandbox) 조직은 기기 id가 `demo-`로 시작해야 하고, 실제 조직은 `demo-`·`sim-`을 쓸 수 없다.
   - `POST /test/api/saas/devices` `{"device_id": "demo-arduino-01"}` → `{"device_token": "..."}`
2. 같은 플랫폼 세션으로 기기를 계획 컨테이너에 바인딩한다. `plan_id`는 그 플랫폼 조직이 만든 계획이어야 한다.
   - `POST /test/api/coldchain/bindings` `{"device_id": "demo-arduino-01", "container_id": "CA-01", "plan_id": "<계획 id>", "stage": "terminal"}`
3. 브리지를 토큰과 함께 돌린다. `--container`·`--plan-id`는 생략한다(주면 바인딩과 같아야 한다).

```bash
export FRESHSWITCH_DEVICE_TOKEN=<1에서 받은 device_token>
uv run python scripts/arduino/bridge.py --fake \
  --api https://freshswitch.ssalhub.xyz/test/api --device-id demo-arduino-01

# 실물 아두이노
uv run --with pyserial python scripts/arduino/bridge.py --port /dev/cu.usbmodem1101 \
  --api https://freshswitch.ssalhub.xyz/test/api --device-id demo-arduino-01 \
  --device-token "$FRESHSWITCH_DEVICE_TOKEN"
```

판독이 들어가면 플랫폼 콜드체인 관제(`GET /test/api/coldchain/containers?plan_id=...`, SSE)가
움직인다. 토큰이 틀리거나 빠지면 `전송 거부 401`, 바인딩이 없거나 컨테이너·계획이 바인딩과
다르면 `전송 거부 422`가 찍힌다.

토큰으로 보낸 측정값은 서버 시각 기준 허용 창 밖이면 그 건만 거부된다(전부 밖이면 `전송 거부 422`).
30일보다 오래된 값은 모두 거부한다. 브리지 시각은 실행한 순간부터 `(배율 - 1)`배로 앞서 나가므로
(`--time-scale 60`이면 실제 1초에 59초) 데모(sandbox) 조직은 서버 시각보다 60일 앞선 값까지 받는다.
`--time-scale 60`으로 쉬지 않고 약 24시간, `--time-scale 120`으로 약 12시간 돌릴 수 있고, 5분 무대
순서는 약 5시간 앞서는 데 그친다. 그보다 오래 돌려 `전송 거부 422`(`60일 넘게 앞섭니다`)가 찍히면
브리지를 다시 켠다(시각이 서버 시각에서 다시 시작한다). 실제 조직은 10분까지만 받으므로 실제 조직
기기로 보낼 때는 `--time-scale 1`을 준다.

## 무대 순서 (`--time-scale 60` 기준 약 5분, 120이면 절반)

1. 브리지 실행, 박스 전원 켜진 상태로 30초 → 트윈 `normal`, 센서 손실률 1.5%
2. 어댑터를 뽑는다 → 즉시 `전원 차단`·`경고` 알림, 10초(=10분) 뒤 터미널 우선 플러그 요청, 30초 뒤 젯셋 배정, 1분 뒤 반입 시간 조정
3. 박스 안 온도가 2℃ 넘게 벗어나면 경고, 4℃ 넘게 벗어나거나 2분(=2시간) 연속 단전이면 위험
4. 4분(=4시간) 뒤 현지 냉장창고 예약 요청이 뜬다. 예상 손실률은 단전 시간만으로 약 3.4%, 박스 온도가 평균 3℃ 넘게 벗어나 있었다면 6~7%까지 오른다
5. 어댑터를 다시 꽂는다 → `전원 복구`, 온도가 돌아오면 `경고 해제`. 손실률은 누적이라 내려가지 않는다

실물 박스는 온도가 천천히 오르므로 손실률은 단전 시간 몫이 먼저 오르고 온도 몫은 늦게 붙는다.
하드웨어 없이 같은 장면을 보려면 `POST /api/coldchain/simulate`(`interval_seconds: 0.5`)나
`bridge.py --fake`를 쓴다.
