import argparse
import getpass
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from freshswitch_backend.data.loader import get_scenario, load_scenarios
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.persistence.database import Database, database
from freshswitch_backend.persistence.orgs import insert_demo_org
from freshswitch_backend.persistence.passwords import hash_password
from freshswitch_backend.persistence.tables import (
    LotRow,
    MembershipRow,
    OrganizationRow,
    PackhouseRow,
    PlanRow,
    ShipperProfileRow,
    UserRow,
)

SCENARIO_ID = "default"
DEMO_PASSWORD_ENV = "DEMO_SEED_PASSWORD"
DEFAULT_DEMO_PASSWORD = "freshswitch-demo"
DEMO_EMAIL_DOMAIN = "demo.freshswitch.ssalhub.xyz"
PLATFORM_PASSWORD_ENV = "PLATFORM_ADMIN_PASSWORD"
MIN_PASSWORD_LENGTH = 8

PLATFORM_ORG = ("freshswitch", "프레시스위치")
FORWARDER_ORGS = (
    ("forwarder-dongnam", "동남 콜드체인 포워딩"),
    ("forwarder-sinhang", "신항 해운 포워딩"),
)


@dataclass(frozen=True)
class DemoOrg:
    id: str
    type: str
    name: str
    user_name: str
    shipper_ids: tuple[str, ...] = field(default=())

    @property
    def user_id(self) -> str:
        return f"user-{self.id}"

    @property
    def email(self) -> str:
        return f"{self.id}@{DEMO_EMAIL_DOMAIN}"


def is_demo_email(email: str) -> bool:
    domain = email.rpartition("@")[2].lower()
    return domain == DEMO_EMAIL_DOMAIN or domain.endswith("." + DEMO_EMAIL_DOMAIN)


def shipper_org_id(shipper_id: str) -> str:
    return "shipper-" + shipper_id.removeprefix("sh-")


def demo_orgs() -> list[DemoOrg]:
    scenario = get_scenario(SCENARIO_ID)
    if scenario is None:
        raise RuntimeError(f"시나리오 {SCENARIO_ID}를 찾을 수 없습니다")
    platform_id, platform_name = PLATFORM_ORG
    orgs = [DemoOrg(platform_id, "platform", platform_name, f"{platform_name} 운영 담당")]
    orgs += [
        DemoOrg(shipper_org_id(s.id), "shipper", s.name, f"{s.name} 담당", (s.id,))
        for s in scenario.shippers
    ]
    orgs += [DemoOrg(oid, "forwarder", name, f"{name} 담당") for oid, name in FORWARDER_ORGS]
    return orgs


def _upsert(db: Session, row: type, values: list[dict]) -> None:
    if values:
        db.execute(insert(row).values(values).on_conflict_do_nothing())


def seed_demo_tenants(db: Session, password: str | None = None) -> None:
    from freshswitch_backend.persistence.sandbox_world import (
        SEED_ID,
        ensure_seed_quality,
        ensure_seed_world,
        mark_seed_members,
    )

    scenario = get_scenario(SCENARIO_ID)
    if scenario is None:
        raise RuntimeError(f"시나리오 {SCENARIO_ID}를 찾을 수 없습니다")
    secret = password or os.environ.get(DEMO_PASSWORD_ENV) or DEFAULT_DEMO_PASSWORD
    orgs = demo_orgs()
    ensure_seed_world(db)
    insert_demo_org(db)
    db.execute(
        insert(OrganizationRow)
        .values(
            [
                {
                    "id": o.id,
                    "type": o.type,
                    "name": o.name,
                    "sandbox": True,
                    "world_id": SEED_ID,
                }
                for o in orgs
            ]
        )
        .on_conflict_do_update(
            index_elements=[OrganizationRow.id], set_={"sandbox": True, "world_id": SEED_ID}
        )
    )

    existing = db.execute(
        select(UserRow.id, UserRow.email).where(
            or_(
                UserRow.id.in_([o.user_id for o in orgs]),
                UserRow.email.in_([o.email for o in orgs]),
            )
        )
    ).all()
    taken = {value for row in existing for value in row}
    _upsert(
        db,
        UserRow,
        [
            {
                "id": o.user_id,
                "email": o.email,
                "password_hash": hash_password(secret),
                "name": o.user_name,
                "is_demo": True,
                "world_id": SEED_ID,
            }
            for o in orgs
            if o.user_id not in taken and o.email not in taken
        ],
    )
    demo_users = set(
        db.scalars(
            select(UserRow.id).where(
                UserRow.id.in_([o.user_id for o in orgs]), UserRow.is_demo.is_(True)
            )
        )
    )
    _upsert(
        db,
        MembershipRow,
        [
            {"user_id": o.user_id, "org_id": o.id, "role": "admin"}
            for o in orgs
            if o.user_id in demo_users
        ],
    )

    packhouses = {p.id: p for p in scenario.packhouses}
    products: dict[str, list[str]] = {}
    for lot in scenario.lots:
        bucket = products.setdefault(lot.shipper_id, [])
        if lot.product_id not in bucket:
            bucket.append(lot.product_id)
    shippers = {s.id: s for s in scenario.shippers}
    owned = [(o.id, shippers[sid]) for o in orgs for sid in o.shipper_ids]
    _upsert(
        db,
        PackhouseRow,
        [
            {
                "id": ph.id,
                "org_id": org_id,
                "name": ph.name,
                "region": ph.region,
                "lat": ph.lat,
                "lon": ph.lon,
                "source": ph.source,
            }
            for org_id, shipper in owned
            for ph in [packhouses[shipper.packhouse_id]]
        ],
    )
    _upsert(
        db,
        ShipperProfileRow,
        [
            {
                "id": shipper.id,
                "org_id": org_id,
                "name": shipper.name,
                "packhouse_id": shipper.packhouse_id,
                "product_ids": products.get(shipper.id, []),
                "source": shipper.source,
            }
            for org_id, shipper in owned
        ],
    )
    mark_seed_members(db, [o.id for o in orgs], [o.user_id for o in orgs])
    ensure_seed_quality(db)


def org_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(OrganizationRow.type, func.count()).group_by(OrganizationRow.type)
    ).all()
    return {kind: count for kind, count in rows}


def run(target: Database | None = None, password: str | None = None) -> dict[str, int]:
    with (target or database).begin() as db:
        seed_demo_tenants(db, password)
        return org_counts(db)


class SeedError(ValueError):
    pass


def _sandbox_profile_orgs(db: Session) -> dict[str, str]:
    rows = db.execute(
        select(ShipperProfileRow.id, ShipperProfileRow.org_id)
        .join(OrganizationRow, OrganizationRow.id == ShipperProfileRow.org_id)
        .where(
            OrganizationRow.type == "shipper",
            OrganizationRow.sandbox.is_(True),
            OrganizationRow.world_id == "seed",
        )
    ).all()
    return {profile_id: org_id for profile_id, org_id in rows}


def _real_plan_lot_ids(db: Session) -> set[str]:
    rows = db.scalars(
        select(PlanRow.payload["registered_lot_ids"])
        .join(OrganizationRow, OrganizationRow.id == PlanRow.org_id)
        .where(OrganizationRow.sandbox.is_(False))
    )
    return {lot_id for ids in rows for lot_id in ids or []}


def _drop_other_scenario_lots(db: Session, scenario_id: str, owner_orgs: set[str]) -> None:
    seeded = {lot.id for scenario in load_scenarios().values() for lot in scenario.lots}
    if not seeded or not owner_orgs:
        return
    referenced = exists().where(
        PlanRow.payload["registered_lot_ids"].contains(func.jsonb_build_array(LotRow.id))
    )
    db.execute(
        delete(LotRow).where(
            LotRow.id.in_(seeded),
            LotRow.shipper_org_id.in_(owner_orgs),
            LotRow.scenario_id != scenario_id,
            LotRow.status == "registered",
            ~referenced,
        )
    )


def _seed_lot_values(db: Session, scenario: Scenario, owners: dict[str, str]) -> list[dict]:
    blocked = _real_plan_lot_ids(db)
    packhouses = {s.id: s.packhouse_id for s in scenario.shippers}
    return [
        {
            "id": lot.id,
            "scenario_id": scenario.id,
            "shipper_id": lot.shipper_id,
            "shipper_org_id": owners[lot.shipper_id],
            "product_id": lot.product_id,
            "pallets": lot.pallets,
            "ready_date": lot.ready_date,
            "destination": lot.destination,
            "sell_by_date": lot.sell_by_date,
            "sell_window": lot.sell_window,
            "packhouse": packhouses.get(lot.shipper_id),
            "source": lot.source,
            "status": "registered",
        }
        for lot in scenario.lots
        if lot.shipper_id in owners and lot.id not in blocked
    ]


def seed_lot_baseline(db: Session, scenario_id: str = SCENARIO_ID) -> list[dict]:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        return []
    return _seed_lot_values(db, scenario, _sandbox_profile_orgs(db))


def seed_lots(db: Session, scenario_id: str = SCENARIO_ID, now: datetime | None = None) -> int:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        raise SeedError(f"시나리오를 찾을 수 없습니다: {scenario_id}")
    owners = _sandbox_profile_orgs(db)
    _drop_other_scenario_lots(db, scenario.id, set(owners.values()))
    stamp = now or datetime.now(UTC)
    values = [
        {**value, "created_at": stamp, "updated_at": stamp}
        for value in _seed_lot_values(db, scenario, owners)
    ]
    if not values:
        return 0
    inserted = db.execute(
        insert(LotRow)
        .values(values)
        .on_conflict_do_nothing(index_elements=[LotRow.id])
        .returning(LotRow.id)
    )
    return len(inserted.all())


def reset_to_baseline(
    db: Session, scenario_id: str = SCENARIO_ID, now: datetime | None = None
) -> int:
    seed_demo_tenants(db)
    return seed_lots(db, scenario_id, now)


def run_lots(target: Database | None = None, scenario_id: str = SCENARIO_ID) -> int:
    with (target or database).begin() as db:
        return seed_lots(db, scenario_id)


@dataclass(frozen=True)
class CreatedPlatform:
    org_id: str
    user_id: str
    email: str


def create_platform(
    db: Session,
    *,
    name: str,
    email: str,
    user_name: str,
    password: str,
    org_id: str | None = None,
) -> CreatedPlatform:
    email = email.strip().lower()
    if "@" not in email or is_demo_email(email):
        raise SeedError("데모 도메인이 아닌 이메일이 필요합니다")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SeedError(f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다")
    org_id = org_id or f"platform-{uuid.uuid4().hex[:12]}"
    if org_id in {o.id for o in demo_orgs()} or db.get(OrganizationRow, org_id) is not None:
        raise SeedError(f"이미 쓰이는 조직 id입니다: {org_id}")
    if db.scalar(select(UserRow.id).where(func.lower(UserRow.email) == email)) is not None:
        raise SeedError(f"이미 가입된 이메일입니다: {email}")
    user_id = f"user-{uuid.uuid4().hex}"
    db.add(OrganizationRow(id=org_id, type="platform", name=name, sandbox=False))
    db.add(
        UserRow(
            id=user_id,
            email=email,
            password_hash=hash_password(password),
            name=user_name,
            is_demo=False,
        )
    )
    db.flush()
    db.add(MembershipRow(user_id=user_id, org_id=org_id, role="admin"))
    db.flush()
    return CreatedPlatform(org_id=org_id, user_id=user_id, email=email)


def _platform_password() -> str:
    secret = os.environ.get(PLATFORM_PASSWORD_ENV)
    if secret:
        return secret
    first = getpass.getpass("플랫폼 관리자 비밀번호: ")
    if first != getpass.getpass("비밀번호 확인: "):
        raise SeedError("비밀번호가 서로 다릅니다")
    return first


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SaaS 데모 조직·계정 시드")
    parser.add_argument("--password", default=None)
    commands = parser.add_subparsers(dest="command")
    platform = commands.add_parser("platform", help="실제(샌드박스 아님) 플랫폼 조직·관리자 생성")
    platform.add_argument("--name", required=True)
    platform.add_argument("--email", required=True)
    platform.add_argument("--user-name", required=True)
    platform.add_argument("--org-id", default=None)
    scenarios = sorted(load_scenarios())
    lots = commands.add_parser("lots", help="sandbox 화주 조직에 시나리오 출하를 등록 상태로 넣기")
    lots.add_argument("--scenario", default=SCENARIO_ID, choices=scenarios)
    reset = commands.add_parser("reset-sandbox", help="sandbox 조직 데이터를 지우고 기준 상태로")
    reset.add_argument("--scenario", default=SCENARIO_ID, choices=scenarios)
    reset.add_argument("--world", default=None)
    reset.add_argument("--all", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "platform":
        try:
            password = _platform_password()
            with database.begin() as db:
                created = create_platform(
                    db,
                    name=args.name,
                    email=args.email,
                    user_name=args.user_name,
                    password=password,
                    org_id=args.org_id,
                )
        except SeedError as exc:
            parser.exit(1, f"{exc}\n")
        print(f"org_id={created.org_id} user_id={created.user_id} email={created.email}")
        return
    if args.command == "lots":
        print(f"lots={run_lots(scenario_id=args.scenario)}")
        return
    if args.command == "reset-sandbox":
        from freshswitch_backend.persistence.sandbox_reset import reset_sandbox

        if not args.world and not args.all:
            parser.exit(2, "world_id 또는 --all 이 필요합니다\n")
        result = reset_sandbox(world_id=args.world, all_worlds=args.all, scenario_id=args.scenario)
        print(
            f"scope=sandbox orgs={result.orgs} reset_at={result.reset_at.isoformat()} "
            f"world_id={result.world_id or ''}"
        )
        return
    with database.begin() as db:
        seed_demo_tenants(db, args.password)
        added = seed_lots(db)
        counts = org_counts(db)
    print(" ".join(f"{kind}={count}" for kind, count in sorted(counts.items())), f"lots={added}")


if __name__ == "__main__":
    main()
