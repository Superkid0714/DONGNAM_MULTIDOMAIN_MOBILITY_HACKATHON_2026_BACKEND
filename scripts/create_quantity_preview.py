import argparse
import json
import secrets
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18110/test/api")
    parser.add_argument("--output", default="/tmp/freshswitch-quantity-preview.json")
    args = parser.parse_args()
    if not args.base_url.startswith("http://127.0.0.1:18110/"):
        raise SystemExit("이 도우미는 격리된 로컬 18110 스테이징 API만 사용합니다")
    suffix = datetime.now(UTC).strftime("%m%d%H%M%S")
    password = f"QuantityPreview-{secrets.token_hex(6)}!"
    now = datetime.now(UTC)
    cutoff = (now + timedelta(days=2, hours=12)).replace(microsecond=0)
    ready = cutoff.astimezone(timezone(timedelta(hours=9))).date().isoformat()
    accounts = {}
    clients = {}
    shipper_clients = {}
    if Path(args.output).exists():
        args.output = str(Path(args.output).with_stem(Path(args.output).stem + "-" + suffix))
    progress = {
        "base_url": args.base_url,
        "created_at": now.isoformat(),
        "accounts": accounts,
        "plans": [],
    }

    def persist():
        Path(args.output).write_text(json.dumps(progress, ensure_ascii=False, indent=2))

    def request(client, method, path, body=None):
        response = client.request(method, path, json=body)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
        for cookie in client.cookies.jar:
            cookie.secure = False
        return response.json() if response.content else None

    for key, role, name in [
        ("forwarder", "forwarder", "수량 검증 포워더"),
        ("shipper_a", "shipper", "취소·충원 검증 화주"),
        ("shipper_b", "shipper", "8파렛트 보전 검증 화주"),
        ("shipper_c", "shipper", "4파렛트 보전 검증 화주"),
    ]:
        client = httpx.Client(
            base_url=args.base_url.rstrip("/") + "/",
            timeout=90,
            headers={"X-Requested-With": "freshswitch"},
        )
        email = f"quantity-{key}-{suffix}@example.com"
        request(
            client,
            "POST",
            "auth/signup",
            {
                "email": email,
                "password": password,
                "name": "수량 정책 검증 담당자",
                "org_name": f"{name} {suffix}",
                "org_type": role,
            },
        )
        clients[key] = client
        accounts[key] = {"email": email, "password": password}
        persist()
        if role == "shipper":
            request(
                client,
                "POST",
                "saas/shipper/packhouses",
                {
                    "name": f"수량 검증 선과장 {key}",
                    "region": "경남 하동",
                    "lat": 35.07,
                    "lon": 127.75,
                    "product_ids": ["strawberry"],
                },
            )
            profile = request(client, "GET", "saas/shipper/profile")["profile"]
            shipper_clients[profile["shipper_id"]] = client
    owner = clients["forwarder"]

    def lot(client, quantity, ready_date=ready):
        return request(
            client,
            "POST",
            "saas/shipper/lots",
            {
                "product": "strawberry",
                "pallets": quantity,
                "ready_date": ready_date,
                "destination": "HKHKG",
            },
        )["registered"]["id"]

    def create_plan(label, initial_quantity=5, days=2.5):
        local_cutoff = (now + timedelta(days=days)).replace(microsecond=0)
        local_departure = local_cutoff + timedelta(days=1, hours=2)
        local_ready = local_cutoff.astimezone(timezone(timedelta(hours=9))).date().isoformat()
        resource = request(
            owner,
            "POST",
            "saas/forwarder/resources",
            {
                "name": f"수량 정책 {label} 컨테이너 {suffix}",
                "kind": "40RH-CA",
                "capacity_pallets": 20,
                "temp_min_c": -1,
                "temp_max_c": 4,
                "vessel_name": f"수량 검증 선박 {label}",
                "terminal": "검증 전용 터미널",
                "origin": "KRPUS",
                "destination": "HKHKG",
                "etd": local_departure.isoformat(),
                "cy_cutoff": local_cutoff.isoformat(),
                "transit_days": 3.5,
                "enabled": True,
            },
        )
        ids = [
            lot(clients["shipper_a"], initial_quantity, local_ready),
            lot(clients["shipper_b"], 8, local_ready),
            lot(clients["shipper_c"], 4, local_ready),
        ]
        plan = request(owner, "POST", "saas/forwarder/plans", {"lot_ids": ids})
        plan_id = plan["plan_id"]
        progress["plans"].append(
            {"label": label, "plan_id": plan_id, "lot_ids": ids, "resource_id": resource["id"]}
        )
        persist()
        for key in ("shipper_a", "shipper_b", "shipper_c"):
            request(
                clients[key], "POST", f"saas/shipper/plans/{plan_id}/confirmation", {"accept": True}
            )
        assignment = next(a for a in plan["plan"]["assignments"] if a["lot_id"] == ids[0])
        if assignment["mode"] != "sea":
            raise RuntimeError(f"해상 배정이 아니므로 검증을 중단합니다: {assignment}")
        path = f"saas/shipper/plans/{plan_id}/lots/{ids[0]}/quantity"
        state = request(clients["shipper_a"], "GET", path)
        if state["policy"]["pilot"]:
            raise RuntimeError("비시범 정책이 필요합니다")
        quote = request(
            clients["shipper_a"], "POST", path + "/quote", {"quantity": initial_quantity - 2}
        )
        expected_fee = 49800 if days < 3 else 0
        if quote["fee_krw"] != expected_fee:
            raise RuntimeError(f"검증 견적이 {expected_fee:,}원이 아닙니다: {quote}")
        return {
            "plan_id": plan_id,
            "voyage_id": assignment["voyage_id"],
            "lot_ids": ids,
            "resource_id": resource["id"],
            "quote": quote,
            "quantity_path": path,
            "ready_date": local_ready,
        }

    def complete_order(plan_id, order_id):
        order = request(owner, "GET", f"plans/{plan_id}/orders/{order_id}")
        for confirmation in order["shipper_confirmations"]:
            if confirmation["status"] == "pending":
                request(
                    shipper_clients[confirmation["shipper_id"]],
                    "POST",
                    f"saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
                    {"accept": True, "shipper_id": confirmation["shipper_id"]},
                )
        request(
            owner,
            "POST",
            f"saas/forwarder/plans/{plan_id}/orders/{order_id}/confirm",
            {"accept": True},
        )

    preview = create_plan("견적")
    refill = create_plan("충원")
    shipper = clients["shipper_a"]
    spare = lot(shipper, 2)
    request(
        shipper,
        "POST",
        f"saas/shipper/plans/{refill['plan_id']}/voyages/{refill['voyage_id']}/refill-waitlist",
        {"lot_id": spare, "quantity": 2},
    )
    quote = request(shipper, "POST", refill["quantity_path"] + "/quote", {"quantity": 3})
    queued = request(
        shipper,
        "POST",
        refill["quantity_path"],
        {"quote_id": quote["quote_id"], "idempotency_key": f"preview-reduce-{suffix}"},
    )
    request_id = queued["change_request_id"]
    change_path = f"saas/forwarder/change-requests/{request_id}"
    computed = request(owner, "POST", change_path + "/replan?llm=false")
    choices = [a for a in computed["replan"]["alternatives"] if a["feasible"]]
    alternative = next((a for a in choices if a["id"] == "sail_as_is"), choices[0])
    applied = request(
        owner,
        "POST",
        change_path + "/apply",
        {"alternative_id": alternative["id"], "idempotency_key": f"preview-apply-{suffix}"},
    )
    order_id = applied["plan"]["active_order_id"]
    complete_order(refill["plan_id"], order_id)
    offers = request(shipper, "GET", "saas/shipper/refill-offers")
    offer = next(
        o for o in offers if o["plan_id"] == refill["plan_id"] and o["status"] == "pending"
    )
    refill.update(
        {
            "spare_lot_id": spare,
            "offer": offer,
            "change_request_id": request_id,
            "order_id": order_id,
        }
    )
    editable = create_plan("설정", days=5)
    air = create_plan("AI항공", initial_quantity=4)
    air_spare = lot(shipper, 4)
    request(
        shipper,
        "POST",
        f"saas/shipper/plans/{air['plan_id']}/voyages/{air['voyage_id']}/refill-waitlist",
        {"lot_id": air_spare, "quantity": 4},
    )
    forecast = request(
        owner,
        "POST",
        f"plans/{air['plan_id']}/delay-forecast?llm=false",
        {"weather": "normal", "congestion": "normal", "quantile": "p90", "min_sell_days": 14},
    )
    if not any(
        p["lot_id"] == air["lot_ids"][0] and p["recommended_airport"]
        for p in forecast["air_switch"]
    ):
        raise RuntimeError("실제 서버 예측에서 항공 추천을 확인하지 못했습니다")
    air_request = request(
        shipper,
        "POST",
        f"saas/shipper/plans/{air['plan_id']}/change-requests",
        {"type": "air_switch", "lot_id": air["lot_ids"][0], "reason": "서버 예측 항공 추천 검증"},
    )
    air_path = f"saas/forwarder/change-requests/{air_request['request_id']}"
    air_computed = request(owner, "POST", air_path + "/replan?llm=false")
    air_choices = [a for a in air_computed["replan"]["alternatives"] if a["feasible"]]
    air_choice = next((a for a in air_choices if a["id"] == "sail_as_is"), air_choices[0])
    air_applied = request(
        owner,
        "POST",
        air_path + "/apply",
        {"alternative_id": air_choice["id"], "idempotency_key": f"preview-air-{suffix}"},
    )
    complete_order(air["plan_id"], air_applied["plan"]["active_order_id"])
    air_state = request(shipper, "GET", air["quantity_path"])
    if air_state["final_fee_krw"] != 0 or not any(
        e.get("exemption") == "ai_air" for e in air_state["events"]
    ):
        raise RuntimeError(f"AI 항공 면제 원장을 검증하지 못했습니다: {air_state}")
    air_offers = request(shipper, "GET", "saas/shipper/refill-offers")
    air["offer"] = next(
        o for o in air_offers if o["plan_id"] == air["plan_id"] and o["status"] == "pending"
    )
    air["forecast_options"] = forecast["options"]
    air["state"] = air_state
    air["spare_lot_id"] = air_spare
    result = {
        "base_url": args.base_url,
        "created_at": now.isoformat(),
        "accounts": accounts,
        "quote_plan": preview,
        "refill_plan": refill,
        "editable_policy_plan": editable,
        "ai_air_plan": air,
        "retention": "검증 계정·자원·출하·계획을 삭제하지 않고 유지합니다",
    }
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "output": args.output,
                "quote_plan": preview["plan_id"],
                "refill_plan": refill["plan_id"],
                "offer_id": offer["offer_id"],
                "fee_krw": quote["fee_krw"],
                "created_at": now.isoformat(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
