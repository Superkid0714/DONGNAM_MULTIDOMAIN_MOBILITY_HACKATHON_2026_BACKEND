from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.intake.service import source_label

INTERNAL_MEMOS = ("발표에서 뺄", "뺄 후보", "근거 미확인", "근거가 못 됨")
DATA_ROOT = Path(__file__).parents[1] / "src" / "freshswitch_backend" / "data"
FORM = {
    "product": "딸기",
    "pallets": 2,
    "ready_date": "2026-11-17",
    "shipper": "산청",
    "destination": "홍콩",
}


def _assert_clean(text: str) -> None:
    for memo in INTERNAL_MEMOS:
        assert memo not in text


def test_source_label_keeps_only_prefix() -> None:
    assert source_label("가정: 0~2°C. 원문 확인 필요") == "가정"
    assert source_label("화주 간편 등록") == "화주 간편 등록"
    assert source_label("") == ""


def test_intake_preview_shows_only_source_label(client: TestClient) -> None:
    response = client.post("/api/intake/lots", json=FORM)
    assert response.status_code == 200
    defaults = response.json()["defaults_applied"]
    sell_by = next(item for item in defaults if item.startswith("sell_by_date"))
    assert sell_by.endswith("(출처: 가정)")
    _assert_clean(response.text)


@pytest.mark.parametrize("scenario_id", ["default", "fuel-shock"])
def test_scenario_response_has_no_internal_memo(client: TestClient, scenario_id: str) -> None:
    response = client.get(f"/api/scenarios/{scenario_id}")
    assert response.status_code == 200
    _assert_clean(response.text)


def test_analysis_responses_have_no_internal_memo(client: TestClient) -> None:
    for path in ("/api/analysis/sensitivity", "/api/analysis/benchmark"):
        response = client.post(path, json={"scenario_id": "default"})
        assert response.status_code == 200
        _assert_clean(response.text)
    business = client.get("/api/analysis/business")
    assert business.status_code == 200
    _assert_clean(business.text)


def test_data_files_have_no_internal_memo() -> None:
    for path in DATA_ROOT.rglob("*.json"):
        _assert_clean(path.read_text(encoding="utf-8"))
