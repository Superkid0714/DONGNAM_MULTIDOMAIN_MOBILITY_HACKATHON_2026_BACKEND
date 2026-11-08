from fastapi.testclient import TestClient
from saas_fixtures import (
    PLATFORM_ORG_ID,
    REAL_PLATFORM_EMAIL,
    REAL_PLATFORM_PASSWORD,
    SAAS_CSRF,
    SHIPPER_ORG_ID,
    create_real_platform,
    login_client,
    password_client,
)

from freshswitch_backend.main import app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import LotRow

PASSWORD = "onboarding-password"
PACKHOUSE = {
    "name": "하동 딸기 선과장",
    "region": "경남 하동",
    "lat": 35.07,
    "lon": 127.75,
    "product_ids": ["strawberry"],
}


def _signup(email: str, org_name: str, org_type: str = "shipper") -> TestClient:
    client = TestClient(app, base_url="https://testserver", headers=SAAS_CSRF)
    response = client.post(
        "/api/auth/signup",
        json={
            "email": email,
            "password": PASSWORD,
            "name": "담당자",
            "org_name": org_name,
            "org_type": org_type,
        },
    )
    assert response.status_code == 201, response.text
    return client


def _lot(product: str = "strawberry", ready: str = "2026-11-17", **extra) -> dict:
    return {
        "product": product,
        "pallets": 6,
        "ready_date": ready,
        "destination": "HKHKG",
        **extra,
    }


def _onboarded(email: str, org_name: str) -> tuple[TestClient, dict]:
    client = _signup(email, org_name)
    created = client.post("/api/saas/shipper/packhouses", json=PACKHOUSE)
    assert created.status_code == 201, created.text
    return client, created.json()


def test_new_shipper_signup_packhouse_lot_reaches_platform_pool(demo_tenants) -> None:
    create_real_platform()
    shipper = _signup("owner@hadong.example.com", "하동 딸기 농장")

    empty = shipper.get("/api/saas/shipper/profile")
    assert empty.status_code == 200
    assert empty.json()["ready"] is False
    assert empty.json()["profile"] is None
    assert {"product_id": "strawberry", "name": "딸기"} in empty.json()["product_options"]
    blocked = shipper.post("/api/saas/shipper/lots", json=_lot())
    assert blocked.status_code == 422

    packhouse = shipper.post("/api/saas/shipper/packhouses", json=PACKHOUSE)
    assert packhouse.status_code == 201, packhouse.text
    ph = packhouse.json()
    assert ph["packhouse_id"].startswith("ph-")
    assert ph["in_use"] is True
    assert (ph["lat"], ph["lon"]) == (35.07, 127.75)

    profile = shipper.get("/api/saas/shipper/profile").json()
    assert profile["ready"] is True
    assert profile["profile"]["name"] == "하동 딸기 농장"
    assert profile["profile"]["packhouse_id"] == ph["packhouse_id"]
    assert profile["profile"]["product_ids"] == ["strawberry"]
    shipper_id = profile["profile"]["shipper_id"]

    lot = shipper.post("/api/saas/shipper/lots", json=_lot())
    assert lot.status_code == 201, lot.text
    registered = lot.json()["registered"]
    assert registered["shipper_id"] == shipper_id
    assert registered["packhouse"] == ph["packhouse_id"]
    assert lot.json()["hints"]["season"]["in_season"] is True

    platform = password_client(app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)
    pool = platform.get("/api/saas/platform/lots").json()
    row = next(x for x in pool if x["id"] == registered["id"])
    assert row["shipper_org_name"] == "하동 딸기 농장"

    catalog = platform.get("/api/saas/catalog").json()
    assert ph["packhouse_id"] in {p["id"] for p in catalog["packhouses"]}

    plan = platform.post("/api/saas/platform/plans", json={"lot_ids": [registered["id"]]})
    assert plan.status_code == 201, plan.text
    scenario = plan.json()["scenario"]
    injected = next(p for p in scenario["packhouses"] if p["id"] == ph["packhouse_id"])
    assert (injected["lat"], injected["lon"]) == (35.07, 127.75)
    assert shipper_id in {s["id"] for s in scenario["shippers"]}
    assert registered["id"] in {a["lot_id"] for a in plan.json()["plan"]["assignments"]}

    sandbox_platform = login_client(PLATFORM_ORG_ID)
    sandbox_pool = sandbox_platform.get("/api/saas/platform/lots").json()
    assert registered["id"] not in {x["id"] for x in sandbox_pool}


def test_profile_create_and_patch() -> None:
    shipper = _signup("owner@goryeong.example.com", "고령 포도")
    first = shipper.post(
        "/api/saas/shipper/packhouses",
        json={"name": "고령 APC", "region": "경북 고령", "lat": 35.72, "lon": 128.26},
    ).json()
    second = shipper.post(
        "/api/saas/shipper/packhouses",
        json={"name": "성주 APC", "region": "경북 성주", "lat": 35.92, "lon": 128.28},
    ).json()
    assert second["in_use"] is False

    no_create = shipper.post(
        "/api/saas/shipper/profile", json={"packhouse_id": first["packhouse_id"]}
    )
    assert no_create.status_code == 405

    patched = shipper.patch(
        "/api/saas/shipper/profile",
        json={
            "name": "고령 샤인머스캣",
            "packhouse_id": second["packhouse_id"],
            "product_ids": ["grape_so2"],
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["packhouse_id"] == second["packhouse_id"]
    assert patched.json()["product_ids"] == ["grape_so2"]
    unknown = shipper.patch("/api/saas/shipper/profile", json={"product_ids": ["mango"]})
    assert unknown.status_code == 422

    moved = shipper.patch(
        f"/api/saas/shipper/packhouses/{first['packhouse_id']}", json={"lat": 35.73}
    )
    assert moved.status_code == 200
    assert moved.json()["lat"] == 35.73
    assert moved.json()["in_use"] is False
    far = shipper.patch(f"/api/saas/shipper/packhouses/{first['packhouse_id']}", json={"lat": 50})
    assert far.status_code == 422
    west = shipper.post("/api/saas/shipper/packhouses", json={**PACKHOUSE, "lon": 120})
    assert west.status_code == 422

    in_use = shipper.delete(f"/api/saas/shipper/packhouses/{second['packhouse_id']}")
    assert in_use.status_code == 409
    removed = shipper.delete(f"/api/saas/shipper/packhouses/{first['packhouse_id']}")
    assert removed.status_code == 204
    listed = shipper.get("/api/saas/shipper/packhouses").json()
    assert [p["packhouse_id"] for p in listed] == [second["packhouse_id"]]


def test_profile_requires_existing_profile_and_own_packhouse() -> None:
    shipper = _signup("owner@miryang.example.com", "밀양 포도")
    missing = shipper.patch("/api/saas/shipper/profile", json={"name": "x"})
    assert missing.status_code == 404
    foreign = shipper.post("/api/saas/shipper/profile", json={"packhouse_id": "ph-jinju"})
    assert foreign.status_code == 405
    assert shipper.get("/api/saas/shipper/profile").json()["profile"] is None


def test_second_packhouse_merges_products_without_moving_profile() -> None:
    shipper, first = _onboarded("owner@merge.example.com", "합치기 농장")
    second = shipper.post(
        "/api/saas/shipper/packhouses",
        json={
            **PACKHOUSE,
            "name": "둘째 선과장",
            "product_ids": ["grape_so2", "strawberry", "grape_so2"],
        },
    )
    assert second.status_code == 201, second.text
    assert second.json()["in_use"] is False
    profile = shipper.get("/api/saas/shipper/profile").json()["profile"]
    assert profile["product_ids"] == ["strawberry", "grape_so2"]
    assert profile["packhouse_id"] == first["packhouse_id"]


def test_profile_packhouse_change_moves_registered_lots_and_guards_delete() -> None:
    shipper, first = _onboarded("owner@moving.example.com", "이전 농장")
    old = first["packhouse_id"]
    planned = shipper.post("/api/saas/shipper/lots", json=_lot()).json()["registered"]
    waiting = shipper.post("/api/saas/shipper/lots", json=_lot(ready="2026-11-18")).json()[
        "registered"
    ]
    with database.begin() as db:
        db.get(LotRow, planned["id"]).status = "planned"
    new = shipper.post(
        "/api/saas/shipper/packhouses", json={**PACKHOUSE, "name": "새 선과장"}
    ).json()["packhouse_id"]

    moved = shipper.patch("/api/saas/shipper/profile", json={"packhouse_id": new})
    assert moved.status_code == 200, moved.text
    lots = {lot["id"]: lot for lot in shipper.get("/api/saas/shipper/lots").json()}
    assert lots[waiting["id"]]["packhouse"] == new
    assert lots[planned["id"]]["packhouse"] == old

    listed = {p["packhouse_id"]: p for p in shipper.get("/api/saas/shipper/packhouses").json()}
    assert listed[old]["in_use"] is True
    assert shipper.delete(f"/api/saas/shipper/packhouses/{old}").status_code == 409

    with database.begin() as db:
        db.get(LotRow, planned["id"]).status = "cancelled"
    assert shipper.delete(f"/api/saas/shipper/packhouses/{old}").status_code == 204
    lots = {lot["id"]: lot for lot in shipper.get("/api/saas/shipper/lots").json()}
    assert lots[waiting["id"]]["packhouse"] == new


def test_other_org_profile_and_packhouses_are_unreachable(demo_tenants) -> None:
    owner, packhouse = _onboarded("owner@a.example.com", "A 농장")
    other, other_packhouse = _onboarded("owner@b.example.com", "B 농장")
    ph = packhouse["packhouse_id"]

    assert other.patch(f"/api/saas/shipper/packhouses/{ph}", json={"name": "x"}).status_code == 404
    assert other.delete(f"/api/saas/shipper/packhouses/{ph}").status_code == 404
    hijack = other.patch("/api/saas/shipper/profile", json={"packhouse_id": ph})
    assert hijack.status_code == 422
    assert ph not in {p["packhouse_id"] for p in other.get("/api/saas/shipper/packhouses").json()}
    view = other.get("/api/saas/shipper/profile").json()
    assert view["profile"]["packhouse_id"] == other_packhouse["packhouse_id"]
    assert [p["packhouse_id"] for p in view["packhouses"]] == [other_packhouse["packhouse_id"]]

    sandbox = login_client(SHIPPER_ORG_ID)
    assert (
        sandbox.patch(f"/api/saas/shipper/packhouses/{ph}", json={"name": "x"}).status_code == 403
    )
    assert (
        sandbox.patch("/api/saas/shipper/packhouses/ph-jinju", json={"lat": 35.0}).status_code
        == 403
    )
    assert sandbox.post("/api/saas/shipper/packhouses", json=PACKHOUSE).status_code == 403
    assert sandbox.patch("/api/saas/shipper/profile", json={"name": "x"}).status_code == 403
    assert sandbox.delete("/api/saas/shipper/packhouses/ph-jinju").status_code == 403
    assert (
        sandbox.post("/api/saas/shipper/profile", json={"packhouse_id": "ph-jinju"}).status_code
        == 405
    )
    sandbox_view = sandbox.get("/api/saas/shipper/profile").json()
    assert sandbox_view["sandbox"] is True
    assert sandbox_view["profile"]["shipper_id"] == "sh-jinju"

    forwarder = _signup("owner@fwd.example.com", "포워더", "forwarder")
    assert forwarder.get("/api/saas/shipper/profile").status_code == 403
    assert forwarder.post("/api/saas/shipper/packhouses", json=PACKHOUSE).status_code == 403

    anonymous = TestClient(app)
    assert anonymous.get("/api/saas/shipper/profile").status_code == 401
    assert owner.get("/api/saas/shipper/profile").json()["profile"]["packhouse_id"] == ph


def test_out_of_season_lot_registers_with_warning() -> None:
    shipper, _ = _onboarded("owner@season.example.com", "시즌 농장")
    summer = shipper.post("/api/saas/shipper/lots", json=_lot(ready="2027-07-01"))
    assert summer.status_code == 201, summer.text
    hints = summer.json()["hints"]
    assert hints["season"] == {
        "product_id": "strawberry",
        "label": "딸기",
        "months": [11, 12, 1, 2, 3, 4, 5],
        "in_season": False,
        "source": "가정(공식 품목별 수출 시기 자료 확인 필요)",
    }
    assert any("딸기 수출 시기(11~5월) 밖" in w and "가정" in w for w in hints["warnings"])
    assert summer.json()["registered"]["status"] == "registered"

    grape = shipper.post("/api/saas/shipper/lots", json=_lot("grape_so2", "2027-04-10"))
    assert grape.status_code == 201, grape.text
    assert grape.json()["hints"]["season"]["in_season"] is False
    assert any("포도 수출 시기(10~3월) 밖" in w for w in grape.json()["hints"]["warnings"])

    winter = shipper.post("/api/saas/shipper/lots", json=_lot(ready="2026-12-01"))
    assert winter.json()["hints"]["season"]["in_season"] is True
    assert not any("수출 시기" in w for w in winter.json()["hints"]["warnings"])


def test_demo_intake_preview_skips_season_check(client: TestClient) -> None:
    for ready in ("2026-11-17", "2027-07-01"):
        response = client.post(
            "/api/intake/lots", json={**_lot(ready=ready), "shipper": "sh-jinju"}
        )
        assert response.status_code == 200, response.text
        assert "season" not in response.json()["hints"]
        assert not any("수출 시기" in w for w in response.json()["hints"]["warnings"])


def test_out_of_season_patch_returns_warning_without_blocking() -> None:
    shipper, _ = _onboarded("owner@season-patch.example.com", "시즌 수정 농장")
    created = shipper.post("/api/saas/shipper/lots", json=_lot())
    assert created.status_code == 201, created.text
    lot_id = created.json()["registered"]["id"]

    summer = shipper.patch(f"/api/saas/shipper/lots/{lot_id}", json={"ready_date": "2027-07-01"})
    assert summer.status_code == 200, summer.text
    assert summer.json()["ready_date"] == "2027-07-01"
    expected = (
        "딸기 수출 시기(11~5월) 밖의 출하일입니다(2027-07-01). 수정은 그대로 됩니다"
        "(출처: 가정(공식 품목별 수출 시기 자료 확인 필요))"
    )
    assert summer.json()["warnings"] == [expected]

    pallets = shipper.patch(f"/api/saas/shipper/lots/{lot_id}", json={"pallets": 4})
    assert pallets.status_code == 200, pallets.text
    assert "warnings" not in pallets.json()

    winter = shipper.patch(f"/api/saas/shipper/lots/{lot_id}", json={"ready_date": "2026-12-01"})
    assert winter.status_code == 200, winter.text
    assert "warnings" not in winter.json()

    shared = shipper.patch(f"/api/lots/{lot_id}", json={"ready_date": "2027-08-03"})
    assert shared.status_code == 200, shared.text
    assert any("딸기 수출 시기(11~5월) 밖" in w for w in shared.json()["warnings"])
    assert "warnings" not in shipper.get(f"/api/lots/{lot_id}").json()


def test_demo_lot_patch_skips_season_check(client: TestClient) -> None:
    created = client.post("/api/lots", json={**_lot(), "shipper": "sh-jinju"})
    assert created.status_code == 201, created.text
    registered = created.json()["registered"]

    response = client.patch(
        f"/api/lots/{registered['id']}",
        json={"ready_date": "2027-07-01", "sell_by_date": "2027-07-08"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ready_date"] == "2027-07-01"
    assert set(response.json()) == set(registered)
