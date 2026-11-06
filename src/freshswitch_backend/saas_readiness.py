import argparse
import asyncio
import json
import os
import secrets
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as clock_time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai import APIConnectionError
from sqlalchemy import Text, cast, delete, func, or_, select

from freshswitch_backend.config import Settings
from freshswitch_backend.explain import ExplainContext, explain
from freshswitch_backend.observability import logger as audit
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import (
    AlertRow,
    BindingRow,
    ChangeRequestRow,
    ContainerRow,
    DeviceRow,
    ForwarderResourceRow,
    LotRow,
    MembershipRow,
    ObservationRow,
    OrganizationRow,
    PackhouseRow,
    PlanConfirmationRow,
    PlanForwarderRow,
    PlanRow,
    RunRow,
    ShipperProfileRow,
    UserRow,
)

DEFAULT_BASE_URL = "http://127.0.0.1:8000/api"
BASE_URL_ENV = "BASE_URL"
CSRF = {"X-Requested-With": "freshswitch"}
ROLES = ("shipper", "forwarder")
EMAIL_DOMAIN = "readiness.freshswitch.invalid"
SENTENCE_LIMIT = 120
KST = timezone(timedelta(hours=9))
LEAD_DAYS = 7
PRODUCT = "strawberry"
DESTINATION = "HKHKG"
PALLETS = 16
PACKHOUSE = {
    "name": "점검용 선과장",
    "region": "경남 진주",
    "lat": 35.18,
    "lon": 128.11,
    "product_ids": [PRODUCT],
}


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
class Purged:
    users: int = 0
    orgs: int = 0
    plans: int = 0
    lots: int = 0
    resources: int = 0
    packhouses: int = 0
    held_orgs: list[str] = field(default_factory=list)
    foreign_plans: list[str] = field(default_factory=list)

    def detail(self) -> str:
        counts = " ".join(
            f"{name}={value}" for name, value in self.__dict__.items() if isinstance(value, int)
        )
        if not self.held_orgs:
            return counts
        return f"{counts} held_orgs={','.join(self.held_orgs)} foreign_plans={','.join(self.foreign_plans)}"


def _emails_filter(emails: Iterable[str]):
    return func.lower(UserRow.email).in_([email.lower() for email in emails])


def purge_accounts(emails: Iterable[str]) -> Purged:
    emails = list(emails)
    if not emails:
        return Purged()
    with database.begin() as db:
        user_ids = list(db.scalars(select(UserRow.id).where(_emails_filter(emails))))
        if not user_ids:
            return Purged()
        candidates = set(
            db.scalars(select(MembershipRow.org_id).where(MembershipRow.user_id.in_(user_ids)))
        )
        outsiders = set(
            db.scalars(
                select(MembershipRow.org_id).where(
                    MembershipRow.org_id.in_(candidates), MembershipRow.user_id.not_in(user_ids)
                )
            )
        )
        org_ids = sorted(
            db.scalars(
                select(OrganizationRow.id).where(
                    OrganizationRow.id.in_(candidates - outsiders),
                    OrganizationRow.sandbox.is_(False),
                )
            )
        )
        held = _foreign_holds(db, org_ids)
        org_ids = [org_id for org_id in org_ids if org_id not in held]
        if held:
            held_members = set(
                db.scalars(select(MembershipRow.user_id).where(MembershipRow.org_id.in_(held)))
            )
            user_ids = [user_id for user_id in user_ids if user_id not in held_members]
        plan_ids = list(db.scalars(select(PlanRow.plan_id).where(PlanRow.org_id.in_(org_ids))))
        lot_ids = list(db.scalars(select(LotRow.id).where(LotRow.shipper_org_id.in_(org_ids))))
        if plan_ids or lot_ids:
            db.execute(
                delete(ObservationRow).where(
                    or_(ObservationRow.plan_id.in_(plan_ids), ObservationRow.lot_id.in_(lot_ids))
                )
            )
        db.execute(delete(PlanRow).where(PlanRow.plan_id.in_(plan_ids)))
        for table in (AlertRow, ContainerRow, BindingRow, RunRow, DeviceRow):
            db.execute(delete(table).where(table.org_id.in_(org_ids)))
        db.execute(delete(LotRow).where(LotRow.id.in_(lot_ids)))
        resources = db.execute(
            delete(ForwarderResourceRow).where(ForwarderResourceRow.org_id.in_(org_ids))
        ).rowcount
        db.execute(delete(ShipperProfileRow).where(ShipperProfileRow.org_id.in_(org_ids)))
        packhouses = db.execute(
            delete(PackhouseRow).where(PackhouseRow.org_id.in_(org_ids))
        ).rowcount
        users = db.execute(delete(UserRow).where(UserRow.id.in_(user_ids))).rowcount
        orgs = db.execute(delete(OrganizationRow).where(OrganizationRow.id.in_(org_ids))).rowcount
    return Purged(
        users=users or 0,
        orgs=orgs or 0,
        plans=len(plan_ids),
        lots=len(lot_ids),
        resources=resources or 0,
        packhouses=packhouses or 0,
        held_orgs=sorted(held),
        foreign_plans=sorted({plan for plans in held.values() for plan in plans}),
    )


def _foreign_holds(db, org_ids: list[str]) -> dict[str, set[str]]:
    own = select(PlanRow.plan_id).where(PlanRow.org_id.in_(org_ids))
    held: dict[str, set[str]] = {}
    links = (
        (PlanConfirmationRow.shipper_org_id, PlanConfirmationRow.plan_id),
        (PlanForwarderRow.forwarder_org_id, PlanForwarderRow.plan_id),
        (ChangeRequestRow.shipper_org_id, ChangeRequestRow.plan_id),
    )
    for org_column, plan_column in links:
        rows = db.execute(
            select(org_column, plan_column).where(org_column.in_(org_ids), plan_column.not_in(own))
        )
        for org_id, plan_id in rows:
            held.setdefault(org_id, set()).add(plan_id)
    lots = list(
        db.execute(
            select(LotRow.id, LotRow.shipper_org_id).where(LotRow.shipper_org_id.in_(org_ids))
        )
    )
    for lot_id, org_id in lots:
        mentions = or_(
            cast(PlanRow.payload, Text).contains(lot_id, autoescape=True),
            cast(PlanRow.committed, Text).contains(lot_id, autoescape=True),
        )
        foreign = db.scalars(
            select(PlanRow.plan_id).where(PlanRow.org_id.not_in(org_ids), mentions)
        )
        for plan_id in foreign:
            held.setdefault(org_id, set()).add(plan_id)
    return held


def leftover_accounts(emails: Iterable[str]) -> int:
    emails = list(emails)
    if not emails:
        return 0
    with database.begin() as db:
        return db.scalar(select(func.count()).select_from(UserRow).where(_emails_filter(emails)))


@dataclass
class Readiness:
    base_url: str
    make_client: Callable[[], httpx.Client]
    keep: bool = False
    out: Callable[[str], None] = print
    results: list[StepResult] = field(default_factory=list)
    clients: dict[str, httpx.Client] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    emails: dict[str, str] = field(default_factory=dict)
    anonymous: httpx.Client | None = None
    tag: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    password: str = field(default_factory=lambda: secrets.token_urlsafe(18))

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

    def _session_client(self, response: httpx.Response) -> httpx.Client:
        cookies = list(response.cookies.jar)
        if not cookies:
            raise StepFailed("no session cookie")
        base_path = urlsplit(self.url("/")).path
        for cookie in cookies:
            if not base_path.startswith(cookie.path.rstrip("/") + "/"):
                raise StepFailed(f"cookie path {cookie.path} does not cover {base_path}")
        client = self.make_client()
        client.headers.update(CSRF)
        client.cookies.clear()
        client.headers["Cookie"] = "; ".join(f"{c.name}={c.value}" for c in cookies)
        return client

    def signup(self, role: str) -> str:
        email = f"{role}-{self.tag}@{EMAIL_DOMAIN}"
        self.emails[role] = email
        body = {
            "email": email,
            "password": self.password,
            "name": "배포 점검",
            "org_name": f"배포 점검 {role} {self.tag}",
            "org_type": role,
        }
        response = self.anonymous.post(self.url("/auth/signup"), json=body, headers=CSRF)
        if response.status_code != 201:
            raise StepFailed(f"signup -> {response.status_code} {response.text[:200]}")
        me = response.json()
        if me["active_org"]["org_type"] != role:
            raise StepFailed(f"signup org_type {me['active_org']['org_type']} != {role}")
        self.state[f"{role}_user"] = me["user"]["id"]
        self.state[f"{role}_org"] = me["active_org"]["org_id"]
        return f"org={me['active_org']['org_id']}"

    def login(self, role: str) -> str:
        login = self.make_client()
        try:
            body = {"email": self.emails[role], "password": self.password}
            response = login.post(self.url("/auth/login"), json=body, headers=CSRF)
        finally:
            login.close()
        if response.status_code != 200:
            raise StepFailed(f"login -> {response.status_code} {response.text[:200]}")
        client = self._session_client(response)
        self.clients[role] = client
        me = self.call(role, "GET", "/me")
        if me["active_org"]["org_id"] != self.state[f"{role}_org"]:
            raise StepFailed("logged in to a different org")
        cookie = client.headers["Cookie"].split("=", 1)[0]
        return f"login=ok cookie={cookie}"

    def account(self, role: str) -> str:
        return f"{self.signup(role)} {self.login(role)}"

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

    def ready_date(self):
        return datetime.now(KST).date() + timedelta(days=LEAD_DAYS)

    def packhouse(self) -> str:
        created = self.call("shipper", "POST", "/saas/shipper/packhouses", PACKHOUSE, expect=201)
        profile = self.call("shipper", "GET", "/saas/shipper/profile")
        if not profile["ready"]:
            raise StepFailed("shipper profile not ready after packhouse")
        self.state["shipper_id"] = profile["profile"]["shipper_id"]
        return f"packhouse={created['packhouse_id']} shipper={self.state['shipper_id']}"

    def register(self) -> str:
        body = {
            "product": PRODUCT,
            "pallets": PALLETS,
            "ready_date": self.ready_date().isoformat(),
            "destination": DESTINATION,
        }
        created = self.call("shipper", "POST", "/saas/shipper/lots", body, expect=201)
        registered = created["registered"]
        self.state.update(lot_id=registered["id"], pallets=registered["pallets"])
        return f"lot={registered['id']} pallets={registered['pallets']}"

    def resource(self) -> str:
        ready = self.ready_date()
        cutoff = datetime.combine(ready, clock_time(16), KST)
        body = {
            "name": f"점검 냉장 컨테이너 {self.tag}",
            "kind": "40RH-CA",
            "capacity_pallets": 20,
            "temp_min_c": -1,
            "temp_max_c": 4,
            "vessel_name": "점검 선박",
            "terminal": "점검 터미널",
            "origin": "KRPUS",
            "destination": DESTINATION,
            "etd": (cutoff + timedelta(days=1, hours=2)).isoformat(),
            "cy_cutoff": cutoff.isoformat(),
            "transit_days": 3.5,
            "enabled": True,
        }
        created = self.call("forwarder", "POST", "/saas/forwarder/resources", body, expect=201)
        return f"resource={created['id']}"

    def create_plan(self) -> str:
        pool = self.call("forwarder", "GET", "/saas/forwarder/lots")
        if self.state["lot_id"] not in {lot["id"] for lot in pool}:
            raise StepFailed("registered lot is not in the forwarder pool")
        body = {"lot_ids": [self.state["lot_id"]]}
        session = self.call("forwarder", "POST", "/saas/forwarder/plans", body, expect=201)
        modes = {a["lot_id"]: a["mode"] for a in session["plan"]["assignments"]}
        if modes.get(self.state["lot_id"]) != "sea":
            raise StepFailed(f"registered lot assignment mode {modes.get(self.state['lot_id'])}")
        self.state["plan_id"] = session["plan_id"]
        return f"plan={session['plan_id']} status={session['status']}"

    def shipper_view(self) -> str:
        plan_id = self.state["plan_id"]
        plans = self.call("shipper", "GET", "/saas/shipper/plans")
        if plan_id not in {p["plan_id"] for p in plans}:
            raise StepFailed("shipper cannot see its plan")
        detail = self.call("shipper", "GET", f"/saas/shipper/plans/{plan_id}")
        rows = {s["shipper_id"] for s in detail["plan"]["settlements"]}
        if not rows <= {self.state["shipper_id"]}:
            raise StepFailed("shipper sees other shippers' settlement rows")
        return f"settlement_rows={len(rows)}"

    def accept_plan(self) -> str:
        plan_id = self.state["plan_id"]
        path = f"/saas/shipper/plans/{plan_id}/confirmation"
        self.call("shipper", "POST", path, {"accept": True})
        visible = self.call("forwarder", "GET", "/saas/forwarder/plans")
        row = next((p for p in visible if p["plan_id"] == plan_id), None)
        if row is None:
            raise StepFailed("forwarder cannot see its plan")
        state = self.call("forwarder", "GET", f"/saas/forwarder/plans/{plan_id}/confirmation")
        if state["status"] != "committed":
            raise StepFailed(f"plan status {state['status']} after shipper accepted")
        return f"status={state['status']}"

    def change_event(self) -> str:
        plan_id = self.state["plan_id"]
        session = self.call("forwarder", "GET", f"/saas/forwarder/plans/{plan_id}")
        containers = {c["id"]: c for c in session["scenario"]["containers"]}
        assignment = next(
            a for a in session["plan"]["assignments"] if a["lot_id"] == self.state["lot_id"]
        )
        voyage_id = containers[assignment["container_id"]]["voyage_id"]
        voyage = next(v for v in session["scenario"]["voyages"] if v["id"] == voyage_id)
        new_etd = datetime.fromisoformat(voyage["etd"]) + timedelta(hours=6)
        event = {"type": "delay", "voyage_id": voyage_id, "new_etd": new_etd.isoformat()}
        body = {"event": event, "idempotency_key": f"readiness-{plan_id}"}
        result = self.call("forwarder", "POST", f"/plans/{plan_id}/events", body)
        if not result["recommended_id"]:
            raise StepFailed("no recommended alternative")
        self.state.update(
            replan_id=result["replan_id"],
            alternative_id=result["recommended_id"],
            explanation=result.get("explanation"),
        )
        return f"voyage={voyage_id} recommended={result['recommended_id']}"

    def apply(self) -> str:
        plan_id = self.state["plan_id"]
        body = {
            "alternative_id": self.state["alternative_id"],
            "replan_id": self.state["replan_id"],
        }
        session = self.call("forwarder", "POST", f"/plans/{plan_id}/apply", body)
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
        self.call("forwarder", "GET", f"/plans/{plan_id}/settlement")
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
        purged = purge_accounts(self.emails.values())
        if purged.held_orgs:
            raise StepFailed(
                f"다른 조직의 계획 {', '.join(purged.foreign_plans)}이 점검용 조직 "
                f"{', '.join(purged.held_orgs)}을 참조해 그 조직은 지우지 않았습니다 ({purged.detail()})"
            )
        signed_up = sum(f"{role}_user" in self.state for role in ROLES)
        if purged.users < signed_up:
            raise StepFailed(f"purged {purged.users} of {signed_up} accounts ({purged.detail()})")
        left = leftover_accounts(self.emails.values())
        if left:
            raise StepFailed(f"{left} readiness accounts left after cleanup")
        return purged.detail()

    def flow(self) -> bool:
        if not self.step("health", self.health):
            return False
        steps = [
            ("shipper signs up", lambda: self.account("shipper")),
            ("shipper registers packhouse", self.packhouse),
            ("forwarder signs up", lambda: self.account("forwarder")),
            ("forwarder registers resource", self.resource),
            ("shipper registers lot", self.register),
            ("forwarder generates plan", self.create_plan),
            ("shipper views plan", self.shipper_view),
            ("shipper accepts plan", self.accept_plan),
            ("forwarder change event", self.change_event),
            ("forwarder applies order", self.apply),
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
            if not self.keep and self.emails:
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
    parser = argparse.ArgumentParser(
        description="SaaS 배포 점검: 점검용 화주·포워더 계정으로 가입부터 정산까지 확인하고, "
        "만든 계정·조직·계획을 DB에서 지운다. 대상 서버와 같은 DB에 붙는 API 컨테이너 안에서 실행한다"
    )
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
