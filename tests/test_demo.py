import re
import shutil
import subprocess

from fastapi.testclient import TestClient

from freshswitch_backend.api import demo, vessels
from freshswitch_backend.config import Settings
from freshswitch_backend.planning.store import store


def test_reset_clears_sessions(client: TestClient, reset_admin_headers) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    assert store.count() >= 1
    response = client.post("/api/demo/reset", headers=reset_admin_headers)
    assert response.status_code == 200
    assert response.json()["cleared_sessions"] >= 1
    assert store.count() == 0
    assert client.get(f"/api/plans/{plan_id}").status_code == 404


def test_status(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        demo,
        "get_settings",
        lambda: Settings(
            offline_mode=True,
            explain_use_llm=False,
            llm_api_key="x",
            llm_model_name="m",
            data_go_kr_service_key="",
        ),
    )
    body = client.get("/api/demo/status").json()
    assert body["offline_mode"] is True
    assert body["explain_use_llm"] is False
    assert body["llm_configured"] is True
    assert body["mof_key_configured"] is False
    assert body["demo_fixture"] is True
    assert "default" in body["scenario_ids"]
    assert "2026-10-30" in [d["date"] for d in body["snapshot_dates"]]
    assert all(d["directions"] == ["I", "O"] for d in body["snapshot_dates"])


def _select(data, path: str):
    for key, index in re.findall(r"\.?([A-Za-z_]\w*)|\[(-?\d+)\]", path):
        data = data[key] if key else data[int(index)]
    return data


def _resolve(value, variables: dict):
    if isinstance(value, dict):
        return {k: _resolve(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, variables) for v in value]
    if isinstance(value, str):
        whole = re.fullmatch(r"\{(\w+)\}", value)
        if whole:
            return variables[whole.group(1)]
        return re.sub(r"\{(\w+)\}", lambda m: str(variables[m.group(1)]), value)
    return value


def _check(actual, op: str, want) -> bool:
    if op == "eq":
        return actual == want
    if op == "contains":
        return want in actual if isinstance(actual, (list, str)) else False
    if op == "len":
        return len(actual) == want
    return actual > want


def test_script_runs_offline_end_to_end(
    client: TestClient, monkeypatch, reset_admin_headers
) -> None:
    monkeypatch.setattr(vessels, "get_settings", lambda: Settings(offline_mode=True))
    steps = client.get("/api/demo/script").json()
    assert [s["step"] for s in steps] == list(range(len(steps)))
    ids = [s["id"] for s in steps]
    assert len(set(ids)) == len(ids)
    assert ids[0] == "reset"
    assert ids[-1] == "quality_impact_shift"
    for required in [
        "intake_strawberry",
        "intake_grape_so2",
        "create_plan",
        "reduce_sancheong",
        "apply_pull_forward",
        "orders_pull_forward",
        "settlement_ca01",
        "air_switch_short_window",
        "confirm_miryang",
        "confirm_forwarder",
        "detect_delay",
        "replan_delay",
        "apply_delay_plan",
        "orders_delay",
        "confirm_delay_forwarder",
        "compare_air",
        "benchmark",
        "business_matrix",
        "arrival_settlement",
        "quality_loss_rates",
        "quality_impact",
    ]:
        assert required in ids
    variables: dict = {}
    for step in steps:
        path = _resolve(step["path"], variables)
        body = _resolve(step["body"], variables)
        response = client.request(
            step["method"],
            path,
            json=body,
            headers=reset_admin_headers if step["id"] == "reset" else {},
        )
        assert 200 <= response.status_code < 300, (step["id"], response.text)
        data = response.json()
        for capture in step["capture"]:
            variables[capture["name"]] = _select(data, capture["path"])
            assert variables[capture["name"]] is not None, (step["id"], capture)
        for check in step["checks"]:
            actual = _select(data, check["path"])
            assert _check(actual, check["op"], check["value"]), (step["id"], check, actual)
    assert store.get(variables["plan_id"]).status == "committed"
    assert store.get(variables["plan_id"]).committed_version == 3


def test_script_curl_format(client: TestClient, tmp_path) -> None:
    response = client.get("/api/demo/script", params={"format": "curl"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-shellscript")
    text = response.text
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "BASE=${BASE:-https://freshswitch.ssalhub.xyz/api}" in text
    steps = client.get("/api/demo/script").json()
    assert text.count("\ncall ") == len(steps)
    assert "capture delay_event -c '.events[0]'" in text
    assert '"event":\'"${delay_event}"\'}' in text
    script = tmp_path / "demo.sh"
    script.write_text(text, encoding="utf-8", newline="\n")
    if bash := shutil.which("bash"):
        result = subprocess.run(
            [bash, "-n", str(script)], check=False, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
