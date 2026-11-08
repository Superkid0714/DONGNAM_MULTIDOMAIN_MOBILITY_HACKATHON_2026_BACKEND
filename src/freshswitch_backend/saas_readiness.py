import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai import APIConnectionError

from freshswitch_backend.config import Settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.explain import ExplainContext, explain
from freshswitch_backend.observability import logger as audit

DEFAULT_BASE_URL = "http://127.0.0.1:8000/api"
BASE_URL_ENV = "BASE_URL"
CSRF = {"X-Requested-With": "freshswitch"}
ROLES = ("platform", "shipper", "forwarder")
SENTENCE_LIMIT = 120


class StepFailed(Exception):
    pass


STEP_ERRORS = (
    StepFailed,
    httpx.HTTPError,
    OSError,
    LookupError,
    AttributeError,
    TypeError,
    ValueError,
    ArithmeticError,
    StopIteration,
    RuntimeError,
)


@dataclass
class StepResult:
    name: str
    passed: bool
    ms: float
    detail: str = ""


@dataclass
class Readiness:
    base_url: str
    make_client: Callable[[], httpx.Client]
    keep: bool = False
    out: Callable[[str], None] = print
    results: list[StepResult] = field(default_factory=list)
    clients: dict[str, httpx.Client] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    anonymous: httpx.Client | None = None

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def call(
        self,
        role: str | None,
        method: str,
        path: str,
        body: Any = None,
        expect: int = 200,
        params: dict[str, Any] | None = None,
    ) -> Any:
        client = self.clients[role] if role else self.anonymous
        if role and f"{role}_org" in self.state:
            self._activate(client, self.state[f"{role}_org"])
        response = client.request(method, self.url(path), json=body, params=params)
        if response.status_code != expect:
            raise StepFailed(f"{method} {path} -> {response.status_code} {response.text[:200]}")
        return response.json() if response.content else None

    def step(self, name: str, fn: Callable[[], str | None]) -> bool:
        started = time.perf_counter()
        try:
            detail = fn() or ""
            passed = True
        except STEP_ERRORS as exc:
            detail = f"{type(exc).__name__}: {exc}"
            passed = False
        ms = round((time.perf_counter() - started) * 1000, 1)
        self.results.append(StepResult(name, passed, ms, detail))
        mark = "PASS" if passed else "FAIL"
        self.out(f"[{mark}] {name:<28} {ms:>9.1f} ms  {detail}".rstrip())
        return passed

    def _activate(self, client: httpx.Client, org_id: str) -> None:
        response = client.request("PUT", self.url("/auth/active-org"), json={"org_id": org_id})
        if response.status_code != 200:
            raise StepFailed(f"active-org {org_id} -> {response.status_code} {response.text[:200]}")

    def login(self, role: str) -> str:
        if "shared" not in self.clients:
            client = self.make_client()
            client.headers.update(CSRF)
            response = client.post(self.url("/auth/demo-login"), json={"new_world": True})
            if response.status_code != 200:
                raise StepFailed(f"demo-login -> {response.status_code} {response.text[:200]}")
            me = response.json()
            cookies = list(response.cookies.jar)
            if not cookies:
                raise StepFailed("demo-login: no session cookie")
            base_path = urlsplit(self.url("/")).path
            for cookie in cookies:
                if not base_path.startswith(cookie.path.rstrip("/") + "/"):
                    raise StepFailed(f"cookie path {cookie.path} does not cover {base_path}")
            client.cookies.clear()
            client.headers["Cookie"] = "; ".join(f"{c.name}={c.value}" for c in cookies)
            for kind in ROLES:
                match = next(item for item in me["memberships"] if item["org_type"] == kind)
                self.state[f"{kind}_org"] = match["org_id"]
                self.clients[kind] = client
            self.clients["shared"] = client
        else:
            client = self.clients["shared"]
        org_id = self.state[f"{role}_org"]
        self._activate(client, org_id)
        return f"org={org_id} cookie_path={client.headers.get('Cookie', '').split('=', 1)[0]}"

    def health(self) -> str:
        body = self.call(None, "GET", "/health")
        self.state["health"] = body
        if body.get("migration_version") != body.get("migration_head"):
            raise StepFailed(
                f"migration {body.get('migration_version')} != head {body.get('migration_head')}"
            )
        if body.get("seed_completed") is False:
            raise StepFailed("seed not completed")
        return (
            f"commit={body.get('commit')} migration={body.get('migration_version')} "
            f"offline={body.get('offline_mode')} llm={body.get('explain_use_llm')}"
        )

    def register(self) -> str:
        catalog = self.call("shipper", "GET", "/saas/catalog")
        self.state["shipper_ids"] = [s["id"] for s in catalog["shippers"]]
        shipper_id = self.state["shipper_ids"][0]
        scenario = get_scenario("default")
        mark = shipper_id.rfind("-sh-")
        bare = shipper_id[mark + 1 :] if mark >= 0 else shipper_id
        lot = next(
            (x for x in scenario.lots if x.shipper_id in {shipper_id, bare}), scenario.lots[0]
        )
        lot_id = f"readiness-{uuid.uuid4().hex[:10]}"
        body = {
            "product": lot.product_id,
            "pallets": lot.pallets,
            "ready_date": lot.ready_date.isoformat(),
            "destination": lot.destination,
            "sell_by_date": lot.sell_by_date.isoformat(),
            "sell_window": lot.sell_window,
            "lot_id": lot_id,
        }
        created = self.call("shipper", "POST", "/saas/shipper/lots", body, expect=201)
        registered = created["registered"]
        self.state.update(lot_id=registered["id"], pallets=registered["pallets"])
        return f"lot={registered['id']} pallets={registered['pallets']}"

    def create_plan(self) -> str:
        body = {"lot_ids": [self.state["lot_id"]]}
        session = self.call("platform", "POST", "/saas/platform/plans", body, expect=201)
        self.state["plan_id"] = session["plan_id"]
        return f"plan={session['plan_id']} status={session['status']}"

    def shipper_view(self) -> str:
        plans = self.call("shipper", "GET", "/saas/shipper/plans")
        if self.state["plan_id"] not in {p["plan_id"] for p in plans}:
            raise StepFailed("shipper cannot see its plan")
        detail = self.call("shipper", "GET", f"/saas/shipper/plans/{self.state['plan_id']}")
        rows = {s["shipper_id"] for s in detail["plan"]["settlements"]}
        if not rows <= set(self.state["shipper_ids"]):
            raise StepFailed("shipper sees other shippers' settlement rows")
        return f"settlement_rows={len(rows)}"

    def assign_forwarder(self) -> str:
        forwarder = self.state["forwarder_org"]
        path = f"/saas/platform/plans/{self.state['plan_id']}/forwarder"
        body = self.call("platform", "PUT", path, {"forwarder_org_id": forwarder})
        visible = self.call("forwarder", "GET", "/saas/forwarder/plans")
        if self.state["plan_id"] not in {p["plan_id"] for p in visible}:
            raise StepFailed("assigned forwarder cannot see the plan")
        return f"forwarder={body['forwarder']['org_id']}"

    def change_event(self) -> str:
        plan_id = self.state["plan_id"]
        session = self.call("platform", "GET", f"/plans/{plan_id}")
        lots = session.get("scenario", {}).get("lots", [])
        target = next(
            (
                item["id"]
                for item in lots
                if item["id"] == "lot-sancheong-sb-1" or str(item["id"]).endswith("-lot-sancheong-sb-1")
            ),
            self.state["lot_id"],
        )
        pallets = next(
            (item.get("pallets", self.state["pallets"]) for item in lots if item["id"] == target),
            self.state["pallets"],
        )
        event = {
            "type": "reduce",
            "lot_id": target,
            "new_pallets": max(1, pallets - 1),
        }
        body = {"event": event, "idempotency_key": f"readiness-{plan_id}"}
        result = self.call("platform", "POST", f"/plans/{plan_id}/events", body)
        if not result["recommended_id"]:
            raise StepFailed("no recommended alternative")
        self.state.update(
            replan_id=result["replan_id"],
            alternative_id=result["recommended_id"],
            explanation=result.get("explanation"),
        )
        return f"recommended={result['recommended_id']}"

    def apply(self) -> str:
        plan_id = self.state["plan_id"]
        body = {
            "alternative_id": self.state["alternative_id"],
            "replan_id": self.state["replan_id"],
        }
        session = self.call("platform", "POST", f"/plans/{plan_id}/apply", body)
        if not session.get("active_order_id"):
            raise StepFailed(f"no execution order (status {session['status']})")
        self.state["order_id"] = session["active_order_id"]
        return f"order={session['active_order_id']} status={session['status']}"

    def shipper_confirm(self) -> str:
        plan_id, order_id = self.state["plan_id"], self.state["order_id"]
        orders = self.call("shipper", "GET", f"/saas/shipper/plans/{plan_id}/orders")
        order = next((o for o in orders if o["order_id"] == order_id), None)
        if order is None:
            raise StepFailed(f"shipper cannot see order {order_id}")
        pending = [c for c in order["shipper_confirmations"] if c["status"] == "pending"]
        if not pending:
            return "no shipper confirmation needed"
        path = f"/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm"
        for confirmation in pending:
            body = {"accept": True, "shipper_id": confirmation["shipper_id"]}
            answered = self.call("shipper", "POST", path, body)
        return f"status={answered['status']}"

    def forwarder_confirm(self) -> str:
        plan_id, order_id = self.state["plan_id"], self.state["order_id"]
        prefix = f"/saas/forwarder/plans/{plan_id}/orders/{order_id}"
        order = self.call("forwarder", "POST", prefix + "/confirm", {"accept": True})
        for request in order["requests"]:
            if request["status"] == "pending":
                order = self.call(
                    "forwarder",
                    "POST",
                    f"{prefix}/requests/{request['request_id']}",
                    {"accepted": True},
                )
        if order["status"] != "executed":
            raise StepFailed(f"order status {order['status']}")
        return f"status={order['status']} requests={len(order['requests'])}"

    def settlement(self) -> str:
        plan_id = self.state["plan_id"]
        rows = self.call("shipper", "GET", "/saas/shipper/settlements")
        mine = [r for r in rows if r["plan_id"] == plan_id]
        if not mine:
            raise StepFailed("shipper settlement row missing")
        platform = self.call("platform", "GET", "/saas/platform/settlements")
        if plan_id not in {r["plan_id"] for r in platform}:
            raise StepFailed("platform settlement missing")
        self.call("platform", "GET", f"/plans/{plan_id}/settlement")
        return f"shipper_rows={len(mine)}"

    def explanation(self) -> str:
        if not self.state["health"].get("llm_configured"):
            raise StepFailed("EXPLAIN_USE_LLM is on but LLM key/model are not configured")
        explanation = self.state.get("explanation")
        if not explanation or not explanation.get("operator_summary"):
            raise StepFailed("no explanation sentence")
        sentence = explanation["operator_summary"].split("\n")[0][:SENTENCE_LIMIT]
        source = explanation["generated_by"]
        if source == "template" and not explanation.get("fallback_reason"):
            raise StepFailed("LLM enabled but template used without fallback_reason")
        reason = explanation.get("fallback_reason")
        return f"source={source} fallback={reason} sentence={sentence!r}"

    def cleanup(self) -> str:
        self.call("platform", "POST", "/saas/sandbox/reset")
        return "reset world"

    def flow(self) -> bool:
        if not self.step("health", self.health):
            return False
        if not self.step("demo-login", lambda: self.login("platform")):
            return False
        steps = [
            ("shipper registers lot", self.register),
            ("platform generates plan", self.create_plan),
            ("shipper views plan", self.shipper_view),
            ("forwarder assignment", self.assign_forwarder),
            ("platform change event", self.change_event),
            ("platform applies order", self.apply),
            ("shipper confirms order", self.shipper_confirm),
            ("forwarder confirms order", self.forwarder_confirm),
            ("settlement lookup", self.settlement),
        ]
        if self.state["health"].get("explain_use_llm"):
            steps.append(("llm explanation", self.explanation))
            steps.append(("llm template fallback", template_fallback))
        for name, fn in steps:
            if not self.step(name, fn):
                return False
        return True

    def run(self) -> bool:
        self.anonymous = self.make_client()
        ok = False
        try:
            ok = self.flow()
        finally:
            if not self.keep and "lot_id" in self.state:
                ok = self.step("cleanup", self.cleanup) and ok
            closed: set[int] = set()
            for client in [self.anonymous, *self.clients.values()]:
                if client is None or id(client) in closed:
                    continue
                closed.add(id(client))
                client.close()
        return ok


class _BrokenCompletions:
    async def create(self, **kwargs):
        raise APIConnectionError(
            message="readiness forced failure", request=httpx.Request("POST", "http://invalid")
        )


def template_fallback() -> str:
    ctx = ExplainContext(
        event_type="reduce",
        event_summary="readiness",
        recommended_alternative_id=None,
        alternatives=[],
        shippers=[],
    )
    settings = Settings(
        llm_api_key="readiness", llm_model_name="readiness", explain_use_llm=True, _env_file=None
    )
    broken = SimpleNamespace(chat=SimpleNamespace(completions=_BrokenCompletions()))
    audit.disabled = True
    try:
        result = asyncio.run(explain(ctx, settings=settings, client=broken))
    finally:
        audit.disabled = False
    if result.generated_by != "template" or not result.operator_summary:
        raise StepFailed("broken LLM provider did not fall back to template")
    return f"fallback={result.fallback_reason}"


def summary(results: list[StepResult]) -> dict[str, Any]:
    return {
        "passed": bool(results) and all(r.passed for r in results),
        "total_ms": round(sum(r.ms for r in results), 1),
        "steps": [r.__dict__ for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SaaS demo-login end-to-end readiness check")
    parser.add_argument("--base-url", default=os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL))
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    readiness = Readiness(
        base_url=args.base_url,
        make_client=lambda: httpx.Client(timeout=args.timeout),
        keep=args.keep,
        out=(lambda line: print(line, file=sys.stderr)) if args.json else print,
    )
    readiness.out(f"SaaS readiness against {args.base_url}")
    readiness.run()
    report = summary(readiness.results)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        verdict = "PASS" if report["passed"] else "FAIL"
        print(
            f"{verdict} {sum(r.passed for r in readiness.results)}/{len(readiness.results)} "
            f"steps, {report['total_ms']} ms"
        )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
