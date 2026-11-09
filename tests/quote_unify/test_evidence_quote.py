from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from freshswitch_backend.api.auth import current_principal
from freshswitch_backend.api.evidence_quote import router
from freshswitch_backend.config import Settings
from freshswitch_backend.main import create_app
from freshswitch_backend.planning.evidence_quote import (
    EvidenceQuoteRequest,
    evaluate_evidence_quote,
)


@pytest.fixture
def payload():
    quote = {
        "provider": "테스트 공급자",
        "mode": "air",
        "scope": {
            "origin": "진주 집하장",
            "destination": "홍콩 수하인",
            "shipment_date": "2026-11-20",
            "delivery_deadline": "2026-11-27",
            "cargo_reference": "동일 출하 8P",
            "pallets": 8,
            "gross_weight_kg": 3500,
            "volume_m3": 12,
            "service_requirements": "지정 온도·도착일 준수",
            "included_services": ["집하", "수하인 인도"],
        },
        "issued_on": "2026-11-09",
        "valid_from": "2026-11-09",
        "valid_until": "2026-11-15",
        "scope_complete": True,
        "capacity_confirmed": True,
        "lines": [{
            "key": "all_in",
            "lower_krw": 1000000,
            "upper_krw": 1000000,
            "status": "confirmed",
            "source_reference": "테스트용 문서 참조; 실제 견적 아님",
        }],
    }
    proposed = deepcopy(quote)
    proposed["mode"] = "sea_shared"
    proposed["lines"][0].update(lower_krw=800000, upper_krw=800000)
    return {"evaluated_on": "2026-11-09", "baseline": quote, "proposed": proposed}


def evaluate(payload):
    return evaluate_evidence_quote(EvidenceQuoteRequest.model_validate(payload))


def test_confirmed_input_fee_excludes_quality(payload):
    payload["expected_quality_loss_krw"] = 999999
    result = evaluate(payload)
    assert result.status == "confirmed_inputs"
    assert result.performance_fee_krw == 50000
    assert result.proposed_customer_charge_krw == 850000
    assert result.net_saving_krw == 150000
    assert result.total_economic_cost_krw == 1849999
    assert not result.price_guaranteed
    assert not result.source_independently_verified


@pytest.mark.parametrize("change", ["expired", "capacity", "estimated", "future"])
def test_conditional_inputs_never_charge_fee(payload, change):
    if change == "expired":
        payload["evaluated_on"] = "2026-11-16"
    elif change == "future":
        payload["proposed"]["issued_on"] = "2026-11-10"
    elif change == "capacity":
        payload["proposed"]["capacity_confirmed"] = False
    else:
        payload["proposed"]["lines"][0]["status"] = "estimated"
    result = evaluate(payload)
    assert result.status == "conditional"
    assert result.performance_fee_krw == 0
    assert result.proposed_customer_charge_krw is None


def test_unknown_cost_suppresses_saving_range(payload):
    payload["proposed"]["lines"][0]["status"] = "unknown"
    assert evaluate(payload).logistics_saving_lower_krw is None


@pytest.mark.parametrize("field,value", [("pallets", 9), ("destination", "다른 도착지")])
def test_scope_mismatch_suppresses_comparison(payload, field, value):
    payload["proposed"]["scope"][field] = value
    result = evaluate(payload)
    assert result.status == "not_comparable"
    assert result.logistics_saving_upper_krw is None
    assert result.performance_fee_krw == 0


def test_service_order_does_not_change_comparability(payload):
    payload["proposed"]["scope"]["included_services"].reverse()
    assert evaluate(payload).status == "confirmed_inputs"


def test_more_expensive_quote_is_not_saving_guarantee(payload):
    payload["proposed"]["lines"][0].update(lower_krw=1100000, upper_krw=1100000)
    result = evaluate(payload)
    assert result.performance_fee_krw == 0
    assert result.net_saving_krw == -100000
    assert not result.price_guaranteed


def test_invalid_bounds_rejected(payload):
    payload["proposed"]["lines"][0]["upper_krw"] = 1
    with pytest.raises(ValidationError):
        evaluate(payload)


def test_api_auth_and_read_only_evaluation(payload):
    app = create_app(Settings(_env_file=None, saas_mode=True))
    client = TestClient(app)
    headers = {"X-Requested-With": "freshswitch"}
    assert client.post("/api/compare/evidence-quote", json=payload, headers=headers).status_code == 401
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    app.dependency_overrides[current_principal] = lambda: object()
    response = client.post("/api/compare/evidence-quote", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["performance_fee_krw"] == 50000