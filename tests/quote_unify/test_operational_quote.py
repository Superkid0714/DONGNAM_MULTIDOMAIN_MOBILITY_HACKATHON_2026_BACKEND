from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from freshswitch_backend.api import operational_quotes as api
from freshswitch_backend.api.auth import current_principal
from freshswitch_backend.planning.operational_quote import OperationalQuote, assess_quote


@pytest.fixture
def payload():
    evidence = {
        "status": "original_checked",
        "reference": "테스트 문서",
        "checked_on": "2026-11-09",
        "applicability": {
            "confirmed": True,
            "carrier": "fixture-carrier",
            "equipment": "air-cargo",
            "valid_from": "2026-11-01",
            "valid_until": "2026-12-31",
            "checked_on": "2026-11-09",
            "reference": "테스트 출하 적용 확인",
            "duplicate_charge_checked": True,
        },
    }
    return {
        "provider": "테스트 포워더",
        "carrier": "fixture-carrier",
        "equipment": "air-cargo",
        "provider_type": "forwarder",
        "mode": "air",
        "origin": "ICN",
        "destination": "HKG",
        "shipment_date": "2026-11-20",
        "cargo": {
            "product": "딸기",
            "packaging": "상자",
            "pallets": 8,
            "gross_weight_kg": "3500",
            "chargeable_weight_kg": "4000",
            "chargeable_weight_evidence": evidence,
            "dimensions": [{"length_cm": 100, "width_cm": 100, "height_cm": 100, "pieces": 8}],
        },
        "issued_on": "2026-11-09",
        "valid_from": "2026-11-09",
        "valid_until": "2026-11-30",
        "state": "confirmed",
        "scope_complete": True,
        "capacity_confirmed": True,
        "original": {
            "filename": "quote.txt",
            "text": "테스트 원문",
            "reference": "문서001",
            "checked_on": "2026-11-09",
        },
        "includes": {"fuel": False, "inland": True, "customs": True, "destination": True},
        "lines": [
            {
                "key": "transport",
                "currency": "KRW",
                "lower": 1000000,
                "upper": 1000000,
                "evidence": evidence,
            }
        ],
        "fuel_surcharge": {
            "origin": "ICN",
            "destination": "HKG",
            "effective_from": "2026-11-16",
            "effective_until": "2026-12-15",
            "applied_on": "2026-11-20",
            "weight_basis": "chargeable",
            "currency": "KRW",
            "rate_per_kg": "100",
            "evidence": evidence,
        },
    }


def assess(payload):
    return assess_quote(OperationalQuote.model_validate(payload), date(2026, 11, 9))


def test_document_checked_amount_and_no_fee(payload):
    result = assess(payload)
    assert result.logistics_billed.amount == 1400000
    assert result.logistics_billed.status == "document_checked"
    assert result.fee is None and result.total_economic_cost is None
    assert not result.price_guaranteed


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirmed", False),
        ("carrier", "other"),
        ("equipment", "other"),
        ("valid_until", "2026-11-19"),
        ("duplicate_charge_checked", False),
    ],
)
def test_original_check_is_not_shipment_applicability(payload, field, value):
    payload["lines"][0]["evidence"]["applicability"][field] = value
    result = assess(payload)
    assert result.logistics_billed.status == "conditional"
    assert result.logistics_billed.amount is None


def test_legacy_evidence_does_not_imply_applicability(payload):
    payload["lines"][0]["evidence"].pop("applicability")
    assert assess(payload).logistics_billed.amount is None


def test_port_cfs_requires_facility_and_all_service_evidence(payload):
    from copy import deepcopy

    payload["mode"] = "ca_sea"
    payload["fuel_surcharge"] = None
    payload["includes"]["fuel"] = True
    payload["stuffing_method"] = "port_cfs"
    assert assess(payload).logistics_billed.amount is None
    payload["stuffing_facility"] = "테스트 시설; 실제 시설 아님"
    evidence = payload["lines"][0]["evidence"]
    payload["port_cfs_services"] = {
        key: deepcopy(evidence)
        for key in ("reefer_pickup", "stuffing", "storage", "terminal_transfer")
    }
    assert assess(payload).logistics_billed.amount == 1000000
    payload["port_cfs_services"].pop("storage")
    assert assess(payload).logistics_billed.amount is None


def test_included_fuel_not_added_twice(payload):
    payload["includes"]["fuel"] = True
    assert assess(payload).logistics_billed.amount == 1000000


@pytest.mark.parametrize("reason", ["missing", "unknown", "mixed", "weight", "route", "date"])
def test_unknown_totals_are_not_fabricated(payload, reason):
    if reason == "missing":
        payload["lines"] = []
    elif reason == "unknown":
        payload["lines"][0]["evidence"] = {"status": "unknown"}
    elif reason == "mixed":
        payload["fuel_surcharge"]["currency"] = "USD"
    elif reason == "weight":
        payload["cargo"]["chargeable_weight_kg"] = None
    elif reason == "route":
        payload["fuel_surcharge"]["destination"] = "SIN"
    else:
        payload["fuel_surcharge"]["applied_on"] = "2026-12-16"
    result = assess(payload)
    assert result.logistics_billed.status == "unknown"
    assert result.logistics_billed.amount is None
    assert result.logistics_billed.lower is None


@pytest.mark.parametrize("reason", ["expired", "capacity", "original", "conditions", "minimum"])
def test_conditional_never_becomes_billed(payload, reason):
    if reason == "expired":
        payload["valid_from"] = "2026-11-10"
    elif reason == "capacity":
        payload["capacity_confirmed"] = False
    elif reason == "original":
        payload["original"] = None
    elif reason == "conditions":
        payload["conditions"] = ["승인 필요"]
    else:
        payload["minimum_pallets"] = 20
    assert assess(payload).logistics_billed.status == "conditional"
    assert assess(payload).logistics_billed.amount is None


def test_api_scope_and_auth(payload, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    assert client.post("/quotes", json=payload).status_code == 401
    principal = SimpleNamespace(org_id="org-a", user_id="u", org_type="forwarder", world_id=None)
    app.dependency_overrides[current_principal] = lambda: principal
    records = {}

    def save(org, actor, quote):
        records[org] = {"id": "q", "created_at": datetime.now(UTC), "quote": quote}
        return records[org]

    monkeypatch.setattr(api, "save_quote", save)
    monkeypatch.setattr(
        api, "read_quote", lambda org, key: records.get(org) if key == "q" else None
    )
    assert client.post("/quotes", json=payload).status_code == 201
    assert client.get("/quotes/q").status_code == 200
    assert (
        client.post("/quotes/q/assessment", json={"evaluated_on": "2026-11-09"}).status_code == 200
    )
    principal.org_id = "org-b"
    assert client.get("/quotes/q").status_code == 404
    principal.world_id = "sandbox"
    assert client.post("/quotes", json=payload).status_code == 403
