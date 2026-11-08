from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID, SAAS_CSRF
from test_forwarder_execution_card import _mixed_plan
from test_saas_api import _assign, _platform_plan

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.execution.card import (
    EnteredNumbers,
    build_execution_card,
    iso6346_check_digit,
    mock_container_no,
)
from freshswitch_backend.main import app


def _number(serial: str, owner: str = "MSKU") -> str:
    code = owner + serial
    return f"{code}{iso6346_check_digit(code)}"


def _card(client: TestClient, plan_id: str) -> dict:
    response = client.get(f"/api/saas/forwarder/plans/{plan_id}/execution-card")
    assert response.status_code == 200, response.text
    return response.json()


def _row(card: dict, container_id: str) -> dict:
    return next(c for c in card["containers"] if c["container_id"] == container_id)


def _put(client: TestClient, plan_id: str, container_id: str, body: dict):
    return client.put(
        f"/api/saas/forwarder/plans/{plan_id}/execution-card/containers/{container_id}",
        json=body,
    )


def _plan(platform_client: TestClient, saas_login, forwarder_client: TestClient):
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    _assign(plan_id, FORWARDER_ORG_ID)
    ids = [c["container_id"] for c in _card(forwarder_client, plan_id)["containers"]]
    assert ids
    return plan_id, ids


def test_saved_numbers_replace_mock(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id, ids = _plan(platform_client, saas_login, forwarder_client)
    number = _number("305438", "CSQU")
    assert number == "CSQU3054383"
    response = _put(
        forwarder_client,
        plan_id,
        ids[0],
        {"booking_no": "hdmu-bk 1234", "container_no": "csqu 305438 3", "seal_no": "SL-998877"},
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["booking_no"] == "HDMU-BK1234"
    assert saved["container_no"] == number
    assert saved["seal_no"] == "SL-998877"
    assert saved["mock"] is False
    assert saved["booking_no_mock"] is False
    assert saved["container_no_mock"] is False
    assert saved["seal_no_mock"] is False
    assert saved["entered_at"] is not None
    card = _card(forwarder_client, plan_id)
    assert _row(card, ids[0]) == saved
    if len(ids) > 1:
        other = _row(card, ids[1])
        assert other["mock"] is True and other["container_no_mock"] is True
        assert card["mock"] is True
    detail = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}").json()
    assert _row(detail["execution_card"], ids[0]) == saved


def test_partial_input_keeps_other_fields_mock(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id, ids = _plan(platform_client, saas_login, forwarder_client)
    before = _row(_card(forwarder_client, plan_id), ids[0])
    response = _put(forwarder_client, plan_id, ids[0], {"booking_no": "BK20261101"})
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["booking_no"] == "BK20261101" and row["booking_no_mock"] is False
    assert row["container_no"] == before["container_no"] == mock_container_no(plan_id, ids[0])
    assert row["seal_no"] == before["seal_no"]
    assert row["container_no_mock"] is True and row["seal_no_mock"] is True
    assert row["mock"] is True
    number = _number("123456")
    second = _put(forwarder_client, plan_id, ids[0], {"container_no": number})
    assert second.status_code == 200, second.text
    row = second.json()
    assert row["booking_no"] == "BK20261101" and row["container_no"] == number
    assert row["seal_no_mock"] is True
    cleared = _put(forwarder_client, plan_id, ids[0], {"booking_no": None})
    assert cleared.status_code == 200, cleared.text
    row = cleared.json()
    assert row["booking_no"] == before["booking_no"] and row["booking_no_mock"] is True
    assert row["container_no"] == number
    assert _row(_card(forwarder_client, plan_id), ids[0]) == row


def test_wrong_check_digit_is_422_with_correct_digit(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id, ids = _plan(platform_client, saas_login, forwarder_client)
    response = _put(forwarder_client, plan_id, ids[0], {"container_no": "CSQU3054384"})
    assert response.status_code == 422
    assert "체크디지트는 3" in response.text and "CSQU3054383" in response.text
    for bad in ("CSQX3054383", "CSQU305438", "CS1U3054383"):
        assert _put(forwarder_client, plan_id, ids[0], {"container_no": bad}).status_code == 422
    assert _put(forwarder_client, plan_id, ids[0], {"booking_no": "B@1"}).status_code == 422
    assert _put(forwarder_client, plan_id, ids[0], {"seal_no": "S" * 21}).status_code == 422
    assert _put(forwarder_client, plan_id, ids[0], {}).status_code == 422
    assert _put(forwarder_client, plan_id, ids[0], {"note": "x"}).status_code == 422
    assert _row(_card(forwarder_client, plan_id), ids[0])["mock"] is True


def test_only_assigned_forwarder_writes(
    platform_client: TestClient,
    saas_login,
    forwarder_client: TestClient,
    other_forwarder_client: TestClient,
    shipper_client: TestClient,
) -> None:
    plan_id, ids = _plan(platform_client, saas_login, forwarder_client)
    body = {"booking_no": "BK-OTHER-1"}
    assert _put(other_forwarder_client, plan_id, ids[0], body).status_code == 404
    assert _put(shipper_client, plan_id, ids[0], body).status_code == 403
    assert _put(platform_client, plan_id, ids[0], body).status_code == 403
    anonymous = TestClient(app, base_url="https://testserver", headers=SAAS_CSRF)
    assert _put(anonymous, plan_id, ids[0], body).status_code in (401, 403)
    assert _put(forwarder_client, plan_id, "CA-NONE", body).status_code == 404
    assert _put(forwarder_client, "plan-missing", ids[0], body).status_code == 404
    assert _row(_card(forwarder_client, plan_id), ids[0])["booking_no_mock"] is True


def test_duplicate_container_no_in_plan_is_409(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id, ids = _plan(platform_client, saas_login, forwarder_client)
    assert len(ids) > 1
    number = _number("777001")
    assert _put(forwarder_client, plan_id, ids[0], {"container_no": number}).status_code == 200
    duplicate = _put(forwarder_client, plan_id, ids[1], {"container_no": number})
    assert duplicate.status_code == 409
    assert ids[0] in duplicate.json()["detail"]
    assert _put(forwarder_client, plan_id, ids[0], {"container_no": number}).status_code == 200
    assert _row(_card(forwarder_client, plan_id), ids[1])["container_no_mock"] is True


def test_saved_numbers_survive_reread(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id, ids = _plan(platform_client, saas_login, forwarder_client)
    body = {"booking_no": "BK-RELOAD-1", "seal_no": "SEAL0001"}
    assert _put(forwarder_client, plan_id, ids[-1], body).status_code == 200
    for _ in range(2):
        again = _row(_card(forwarder_client, plan_id), ids[-1])
        assert again["booking_no"] == "BK-RELOAD-1" and again["seal_no"] == "SEAL0001"
        assert again["booking_no_mock"] is False and again["container_no_mock"] is True


def test_numbers_of_removed_container_are_hidden() -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    plan = _mixed_plan(scenario, {"lot-jinju-sb-1": 5})
    gone = _number("999999")
    entered = {
        "CA-01": EnteredNumbers(booking_no="BK-KEPT-1"),
        "CA-GONE": EnteredNumbers(container_no=gone),
    }
    card = build_execution_card("plan-x", 2, scenario, plan, entered=entered)
    assert [c.container_id for c in card.containers] == ["CA-01"]
    row = card.containers[0]
    assert row.booking_no == "BK-KEPT-1" and row.booking_no_mock is False
    assert row.container_no_mock is True and row.mock is True
    assert gone not in card.model_dump_json()
