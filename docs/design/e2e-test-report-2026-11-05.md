# 실제 화면 E2E 검증 — 2026-11-05

**상태: 검증·수정·배포 완료. 테스트 데이터는 모두 보존했습니다.**

## 현재 결론

실제 LIVE UI에서 **화주 가입 → 품목·출하 장소·출하2건 등록 → 포워더 가입·자기 컨테이너 등록 → 대상 출하만 미리 계산·계획 생성 → 화주 초기 수락 → 직접 변경 재계산·적용 → 화주 변경 재확인 → 포워더 내부 실행확정**을 완료했습니다.

최종 계획은 `v2 / committed`, 실행 주문은 `forwarder_confirmed`입니다. 선사·항공사 회신3건은 모두 `pending`으로 남겼습니다. **외부 예약·실제 운송·결제 완료를 검증한 것은 아닙니다.** 외부 이메일/운송사/결제 요청을 보내지 않았고, 앱의 모의 알림·내부 상태만 바꿨습니다.

사용자 지시에 따라 모든 테스트 데이터와 실패 중간 상태를 보존했습니다. 자동 정리는 실행하지 않습니다. 기존 느엥 고객 데이터19개 테이블의 대상별 행 수와 해시가 테스트 전후 정확히 같음을 읽기 전용으로 확인했습니다.

## 버전과 실행 환경

- 대상: https://freshswitch.ssalhub.xyz/test
- 업무 흐름 실행 web: `saas-pretendard-20261104-r1`
- API: `saas-product-archives-20261104-r1`, migration `0016` (이번 검증에서 API 재배포 없음)
- 최종 배포 web: `saas-e2e-fixes-20261105-r2` —2026-11-04 19:02:53 UTC(11/05 04:02 KST), production build/TypeScript39개 경로 및 영향 구간 preview/LIVE 통과
- 안정 기반: `/home/hajinkwonsee/freshswitch-releases/pretendard-final`
- 패치 스냅샷: `/home/hajinkwonsee/freshswitch-releases/e2e-fixes-final` (검증된9파일만 덧붙임)
- 사용 도구: `/home/hajinkwonsee/developments/venv`의 Python Playwright + 설치된 Chromium headless shell156
- 주 증거: `/home/hajinkwonsee/freshswitch-releases/e2e-20261105-full/`
- 비밀번호·세션 쿠키·토큰·장치 인증 정보는 문서/상태 manifest에 기록하지 않습니다.

기본 Chromium151은 새 페이지 생성에서 멈췄고 독립 점검에서도 같은 현상을 확인했습니다. 서비스 오류가 아니며 쓰기 발생 전 브라우저 바이너리만156으로 바꿨습니다. 초반 이벤트 기록 함수/옛 selector도 보정한 뒤 같은 객체를 재사용했습니다.

## UI로 수행한 재현 단계와 결과

| 단계 | 실제 동작/확인 | 결과·증거 |
|---|---|---|
| 1 | 가입 화면에서 화주 업체/담당자/이메일 입력 | 가입 성공; 별도 합성 조직 |
| 2 | 품목 관리에서 전용 과실 등록(0~2℃,14일,415kg/P) | `01-product-created.png` |
| 3 | 출하 장소 이름·주소·부산 좌표35.1/129.0·취급 품목 입력 | 등록/지도 표시 성공; `02-pickup-created.png` |
| 4 | 출하 모달에서8P·홍콩·11/16을 두 번 등록, 수량 전체 삭제 후 입력 | 두 건 모두 등록; `03-shipment-*-registered.png`, `04-own-two-lots.png` |
| 5 | 포워더 가입 후20P CA 공간·0~5℃·11/17 18:00 반입·11/18 18:00 출항·3일 등록 | 자기 자원 생성; `05-own-resource.png` |
| 6 | 풀에서 두 합성 lot ID의 체크박스만 선택 | 미리 계산/계획 생성 성공; POST/preview payload에 본인 ID만 허용하는 관측 가드 사용; `06-preview-own-fixtures-only.png` |
| 7 | 초기 계획의16P가 자기 컨테이너에 SEA 배정됨 확인, 화주가 계획 수락 | `pending_shipper_confirmation → committed`; `07~09-*.png` |
| 8 | 자기 화물 콜드체인을 화주/포워더 화면에서 읽기 |35분8점·최고1.25℃ 그래프; `11-*-owned-coldchain.png` |
| 9 | 포워더 계획 상세에서 항공 전환 재계산 후 대안 적용 | 실행 주문 생성; `12-three-replan-alternatives.png` |
| 10 | 포워더 실행 버튼이 화주 동의 전 비활성인지 확인 | 차단됨; `13-execution-blocked-until-shipper.png` |
| 11 | 화주가 영향을 받는 변경을 다시 수락 | `shipper_confirmed`; `14~15-*.png` |
| 12 | 포워더가 내부 실행확정 | `forwarder_confirmed`; 외부 회신3건 대기; `16~17-*.png` |
| 13 | 정산과 전환 후 현재 화물 화면 확인 | 예상 절감/수수료0원으로 갱신; 현재 SEA 없음이 정상; `19~20-*.png` |

장치/바인딩과 합성 품온8점은 **API로 준비한 보조 데이터**입니다. UI로 장치 설치나 실제 센서 수집을 검증했다고 주장하지 않습니다. API 보조 준비는 자기 E2E 계획/컨테이너에만 수행했고 UI의 합성 출처 표시를 유지했습니다. 거래/승인/재계산 단계는 API로 우회하지 않았습니다.

### 두 출하가 모두 항공으로 바뀐 이유

직접 항공 전환을 지정한 출하는 첫 lot뿐입니다. 나머지8P도 항공이 된 것은 자기 컨테이너가 잘못 제외돼서가 아니라 비용 비교 결과입니다. 관측자가 저장된 대안을 읽어 확인했습니다.

- 초기16P 해상 예상 원가: **12,524,058원**
- 선택된16P 항공 예상 원가: **16,750,640원**
- 자기 컨테이너를 쓰는8P항공+8P해상 대안: 실행 가능, **19,207,978원**
- 선택안은 혼합안보다 **2,457,338원** 낮습니다. 직접 지정하지 않은 lot의 이유는 `capacity_or_cost`입니다.

모두 계획 모델의 견적이며 실청구액이 아닙니다. 초기 UI의 예상 절감4,226,582원/예상 수수료1,056,646원은 변경 후0원으로 갱신됐습니다. 현재 계획이 전부 항공이므로 기존 해상 그래프를 현재 해상 운송인 것처럼 계속 보여주지 않는 것도 정상입니다. 기존 합성 장치·바인딩·측정값은 남아 있습니다.

## 발견한 UI 문제와 수정

| 항목 | 재현/원인 | 조치·검증 상태 |
|---|---|---|
| 포워더 풀 상태 필터 | 실제 UI의 ‘계획 포함’ 버튼이 허용되지 않는 `status=planned`를 전송해422, 화면은 서버 연결 실패로 표시 | 포워더에게 해당 상태 탭을 숨기고 미배정 등록 출하만 표시한다는 안내.1440/390px preview:422·페이지 넘침0 |
| 예상 정산의 실현액 같은 표현 | 운송 전인데 ‘내가 아낀 금액과 낸 수수료’, ‘실제 내는 금액’ 표시 | 예상 정산/비용/절감/수수료로 명시. 검수 반영 환급도 입금 완료와 구별; 1440/390px preview와 LIVE 확인 통과 |
| 이전 플랫폼 운영 역할 문구 | 한 화주 계획인데 플랫폼이 다른 화주와 묶었다는 설명 | 포워더의 계획 제안/시스템 재검토로 교정; 임의의 다른 화주 원인을 만들지 않음 |
| 달력 KST 오늘 불일치 | UTC15시 이후 등록 기본일은 KST 다음 날, 달력 footer/오늘 버튼은 브라우저 전날 | 공용 KST today helper 사용. UTC/서울/LA 브라우저,18개 날짜 경계·선택값 유지·범위 제한 검사 통과; 실제 폼 preview에서도 일치 |

독립 역할/반응형 점검은3역할50개 탐색과 실제 고객 읽기 전용24개 화면에서 넘침·JS 오류·업무 쓰기0건을 확인했습니다. 빠른 전체 reload 반복에서는 같은 로컬 IP의 nginx `20req/s, burst50` 한계로429가11번 나왔습니다. nginx 로그와 일치했고, 정상 속도 SPA4단계 재검증은429/오류0건이어서 정책을 완화하지 않았습니다. 모달 focus trap, 모바일 메뉴/계정, 상품 dropdown, calendar 키보드/Escape,2D/3D geometry도 별도 읽기 전용 점검했습니다.

## 보존한 테스트 데이터

테스트 접두사: `20261105-033547`. 아래는 모두 합성 업무 데이터입니다. 삭제하지 않습니다.

| 종류 | ID/계정 | 최종 용도·상태 |
|---|---|---|
| shipper 계정 | `flow-e2e-20261105-033547-shipper@example.com` | `E2E 격리 검증 20261105-033547 화주` |
| shipper 조직/사용자 | `shipper-ee90a20db866` / `user-005fbd3f7e234ab480df746ca3890ba1` | 합성 계정 보존 |
| forwarder 계정 | `flow-e2e-20261105-033547-forwarder@example.com` | `E2E 격리 검증 20261105-033547 포워더` |
| forwarder 조직/사용자 | `forwarder-aa5dcef004af` / `user-9a0ae728ac7b4a19b36ec10bc75384f5` | 합성 계정 보존 |
| 전용 품목 | `prd-3ac2f5525c524a41b8cb988a81af88e8` | 보존·새 출하 선택 가능 |
| 출하 장소 | `ph-40024cfe479f` | 부산 가상 집하 장소·보존 |
| 출하8P | `lot-57d17026bb03-prd-3ac2f5525c524a41b8cb988a81af88e8-1` | 계획 포함, 현재 AIR; 실제 운송 완료 아님 |
| 출하8P | `lot-57d17026bb03-prd-3ac2f5525c524a41b8cb988a81af88e8-2` | 계획 포함, 현재 AIR; 실제 운송 완료 아님 |
| 자원20P | `fr-911edf125af04404b61873fc` | 활성·배정 해제·가용20P(모두 AIR 변경 후) |
| 계획 | `3897fe48-34d3-4d9b-8bc0-84428371a234` | v2 / committed / AIR8+8 |
| 재계산 | `d46b5a6b-de48-481e-b937-f807d86569b7` | 적용 대안 비교·이력 보존 |
| 실행 주문 | `0c6d7795-05df-4bbd-9dfd-d5efc7df68ba` | forwarder_confirmed, 외부3건 pending |
| 합성 센서 | `synthetic-demo-e2e-3897fe4834d3-01` | 바인딩1개·측정8개·실측 아님 |
| 읽기 전용 방문자 | `wd16f4507f886` / `user-wd16f4507f886` |8개 역할 조직/seed data 보존; 업무 데이터 변경 없음 |

객체를 생성한 뒤 하네스 오류로 중단된 구간은 같은 manifest로 재개했습니다. 별도 가입 객체를 중복 생성하지 않았습니다. 상태 이력·안전 가드·생성 ID는 `manifest.json`, 최종 backend 관측은 `backend-observer-final-state.json`에 있습니다.

## 확인 범위와 제한

- 검증한 것은 앱 내부의 제안·승인·재계산·내부 실행확정입니다. 실제 선사/항공사 예약, 운송·통관, 센서 하드웨어, 결제·입금은 미실행입니다.
- 별도의 화주 자발적 변경 요청 생성 UI 경로는 이번 주 흐름에 포함하지 않았습니다. 포워더 직접 재계산 경로의 화주 재확인과 실행확정은 UI로 통과했습니다. 정상 인증 화주 요청에 대한 backend 동의 경로는 앞선 독립 API 회귀에서 통과했습니다.
- 서버 통합/API 불변식 검사는 기존 작업에서 별도로 통과했으며 이번 실제 UI 시험과 혼합해 전체 UI 통과라고 부르지 않습니다.
- 보존된 계정에 재접속해 화면을 확인할 수 있습니다. 이미 적용된 수락/변경을 같은 데이터에서 재실행하면 다른 상태를 만들 수 있으므로 신규 재현은 다른 접두사로 구분합니다.
- main 디렉터리·main 서비스·백엔드 배포·고객 업무 데이터는 변경하지 않았습니다. commit/push도 하지 않았습니다.

## 주요 증거 파일

- `journey.py`, `manifest.json`: 실제 UI 절차와 객체/이벤트 이력(문서에 인증 상태 파일을 포함하지 않음)
- `backend-observer-telemetry.json`: API로 준비한 합성 측정의 명시적 출처
- `backend-optimization-review.json`: 비용 비교와 자기 자원 제외 오류가 아님을 확인한 근거
- `backend-observer-final-state.json`: 최종 계획·주문·자원/장치 상태
- `/home/hajinkwonsee/freshswitch-releases/e2e-public-integrity-comparison-20261105.json`: 기존 고객19개 테이블 행 수/해시 동일
- `/home/hajinkwonsee/freshswitch-releases/e2e-ui-20261104/ui-readonly-summary.json`: 독립 역할/모바일 점검과429 분류
- `fix-proof-preview.json`: 풀 필터·실제 UTC 브라우저 KST today 확인(업무 쓰기0건)

## 최종 배포와 재확인

`saas-e2e-fixes-20261105-r2` 배포 후 실제 HTTPS에서 포워더 풀1440/390px, 금지 필터 없음/422없음/페이지 넘침0, UTC 브라우저의 등록 기본일·달력 footer·오늘 선택값 일치를 확인했습니다. 정산/실제 대기 계획 문구도1440/390px에서 읽기 전용 재확인했습니다. 양수 환급의 ‘검수 반영 계산’은 별도 응답 렌더 fixture로 확인했으며 실제 고객 DB를 바꾸지 않았습니다.

이전 `saas-pretendard-20261104-r1` 이미지를 보존했습니다. 임시18109 web preview 컨테이너만 중지했으며, 계정·출하·자원·계획·주문·장치·측정값은 삭제하지 않았습니다. API 및 main API/web의 시작 시각과 버전은 바꾸지 않았습니다. 세션/인증 정보를 노출하지 않았고 commit/push도 실행하지 않았습니다.

- `fix-proof-live.json`: 풀/KST 재검증, 업무 쓰기0건
- `/home/hajinkwonsee/freshswitch-releases/e2e-ui-20261104/copy-live-results.json`: 실제 문구/반응형 재검증
- 수정 파일9개는 패치 스냅샷의 `e2e-patch-files.json`에 열거했습니다.


## 배포 후 최종 무결성

2026-11-04 19:04:27 UTC 재관측에서도 기존 고객19개 대상 테이블의 SHA256와 행 수가 초기18:35 기준과 정확히 일치하고 고객 계획 ID도 같습니다. 자기 E2E 계획은 v2/committed, 주문은 forwarder_confirmed, 외부3건은 pending으로 유지됐습니다. 자기 자원 가용20P·합성 바인딩1개·품온8점도 남아 있습니다. API 재배포/DB 변경 없이 이번 UI 패치만 배포했으며, 배포 후 backend5xx와 포워더 풀422는0건입니다.

읽기 전용 방문자 세계의 보존 조직 ID:

- `wd16f4507f886-freshswitch`
- `wd16f4507f886-shipper-jinju`
- `wd16f4507f886-shipper-sancheong`
- `wd16f4507f886-shipper-miryang`
- `wd16f4507f886-shipper-sangju`
- `wd16f4507f886-shipper-pohang`
- `wd16f4507f886-forwarder-dongnam`
- `wd16f4507f886-forwarder-sinhang`
