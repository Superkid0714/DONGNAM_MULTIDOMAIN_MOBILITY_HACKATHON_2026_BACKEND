# 부산항 컨테이너선 ML 이력 CSV

파일명: `busan_container_voyages.csv`  
기대 위치: `src/freshswitch_backend/data/history/busan_container_voyages.csv`  
Git 추적: 제외 (`.gitignore`에 등록)

## 준비 방법

현재 개발용 파일은 모델 제공자가 전달한 `voyages.csv`입니다. 프로젝트에 합류한 개발자는 동일한 학습 데이터 내보내기 파일을 전달받아 위 경로에 복사해야 합니다. 이 파일은 이 저장소에 없으며, 별도 공개 다운로드 URL도 확인되지 않아 URL을 임의로 안내하지 않습니다.

Colab 학습 산출물에서 동일한 전처리 결과 `voyages.csv`를 내려받은 뒤, Windows PowerShell에서 저장소 루트 기준 아래처럼 복사합니다.

```powershell
Copy-Item "$env:USERPROFILE\Downloads\voyages.csv" `
  "src/freshswitch_backend/data/history/busan_container_voyages.csv"
```

현재 저장소에는 49,628항차 학습 CSV를 생성하는 Colab 노트북/전처리 내보내기 코드가 없으므로, 원본 CSV가 없으면 학습 데이터 담당자에게 동일한 내보내기 파일 또는 재생성 절차를 받아야 합니다.

데이터의 원천은 공공데이터포털 해양수산부 선박입출항정보 `VsslEtrynd5/Info5`(부산항 코드 `020`)로 기록되어 있습니다. 서비스 키를 이용한 API 조회는 코드의 `python -m freshswitch_backend.forecast.history --start YYYYMMDD --end YYYYMMDD` 도구로 별도의 JSON 이력을 수집할 수 있지만, 해당 산출물은 학습 CSV와 형식·필터가 다르므로 이 CSV를 그대로 재생성하지는 않습니다. 동일한 모델 입력 데이터를 다시 만들려면 모델 학습 담당자가 사용한 전처리/내보내기 과정을 받아야 합니다.

## 필수 컬럼

모델 로더가 확인하는 컬럼은 `clsgn`, `etryptDt`, `tkoffDt`, `delay_h`, `satmntEntrpsNm`입니다. 피처 계산기는 추가로 `laidupFcltyNm`, 선박 종류·국적·항로, 톤수, 예선·도선 여부 등을 사용합니다. 원본 헤더와 피처 계산 정의를 바꾸지 말고 UTF-8 CSV로 보관하세요.
