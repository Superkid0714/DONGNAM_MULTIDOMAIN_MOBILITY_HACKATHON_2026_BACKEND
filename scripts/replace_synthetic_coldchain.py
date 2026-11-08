import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from alembic.script import ScriptDirectory
from dotenv import dotenv_values
from sqlalchemy import create_engine, delete, func, or_, select, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session

from freshswitch_backend.coldchain import ColdchainParams, coldchain_store
from freshswitch_backend.coldchain.models import Reading
from freshswitch_backend.coldchain.telemetry import reefer_device_id, reefer_trace, seed_container
from freshswitch_backend.coldchain.twin import build_context, evaluate, loss_summary
from freshswitch_backend.domain.container_numbers import container_number, is_iso6346
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.database import migration_config
from freshswitch_backend.persistence.tables import (
    BindingRow,
    ContainerRow,
    DeviceRow,
    ForwarderResourceRow,
    LotRow,
    OrganizationRow,
    PlanRow,
    ReadingRow,
    RunRow,
)
from freshswitch_backend.planning.store import PlanSession

BACKEND = Path(__file__).resolve().parents[1]
SYNTHETIC_ID = re.compile(r"^synthetic-", re.IGNORECASE)
SYNTHETIC_NAME = ("합성", "실제 센서 아님", "시연용")
RENAME_TABLES = (
    "plan_sessions",
    "plan_orders",
    "plan_pending_replans",
    "plan_idempotency_keys",
    "plan_change_requests",
    "plan_shipper_confirmations",
    "notifications",
    "app_meta",
    "forwarder_resources",
)
BACKUP_TABLES = (
    "devices",
    "coldchain_bindings",
    "coldchain_containers",
    "coldchain_readings",
    "coldchain_alerts",
    "coldchain_sim_runs",
    *RENAME_TABLES,
)
API_FILES = ("coldchain/twin.py", "coldchain/telemetry.py", "domain/container_numbers.py")


@dataclass
class Target:
    owner_org_id: str
    plan_id: str
    old_container_id: str
    new_container_id: str
    record_key: str
    devices: list[str]
    readings: int
    alerts: int
    session: PlanSession
    stage: str
    occurrences: dict[str, int] = field(default_factory=dict)

    @property
    def seed(self) -> str:
        return f"{self.plan_id}:{self.new_container_id}"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="화주 조직의 합성 콜드체인 기록을 실제 리퍼 형식 측정값으로 바꾼다"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--shipper-org-id")
    parser.add_argument("--shipper-org-name", default="느엥")
    parser.add_argument("--env-file", default=str(BACKEND / ".env.saas"))
    parser.add_argument("--db-container", default="freshswitch-saas-db-1")
    parser.add_argument("--api-container", default="freshswitch-saas-api-1")
    parser.add_argument("--backup-dir", default=str(Path.home() / "freshswitch-backups"))
    parser.add_argument("--skip-api-check", action="store_true")
    return parser.parse_args()


def _docker(*args: str, stdout=subprocess.PIPE) -> str:
    done = subprocess.run(["docker", *args], check=True, stdout=stdout, text=True, timeout=300)
    return done.stdout.strip() if isinstance(done.stdout, str) else ""


def _url(args: argparse.Namespace, read_only: bool) -> URL:
    env = dotenv_values(args.env_file)
    host = _docker(
        "inspect",
        "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
        args.db_container,
    ).split()[0]
    query = {"options": "-c default_transaction_read_only=on"} if read_only else {}
    return URL.create(
        "postgresql+psycopg",
        username=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        host=host,
        port=5432,
        database=env["POSTGRES_DB"],
        query=query,
    )


def _check_schema(engine: Engine) -> None:
    head = ScriptDirectory.from_config(migration_config()).get_current_head()
    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    if current != head:
        sys.exit(f"DB 스키마 {current}와 로컬 코드 {head}가 달라 중단합니다")


def _check_api(args: argparse.Namespace) -> None:
    root = _docker(
        "exec",
        args.api_container,
        "python",
        "-c",
        "import freshswitch_backend,os;print(os.path.dirname(freshswitch_backend.__file__))",
    )
    local = BACKEND / "src" / "freshswitch_backend"
    for name in API_FILES:
        try:
            remote = _docker("exec", args.api_container, "md5sum", f"{root}/{name}").split()[0]
        except subprocess.CalledProcessError:
            sys.exit(f"API 컨테이너에 {name}이 없습니다. 이 변경으로 다시 배포한 뒤 실행하세요")
        if remote != hashlib.md5((local / name).read_bytes()).hexdigest():
            sys.exit(
                f"API 컨테이너의 {name}이 로컬과 다릅니다. 이 변경으로 다시 배포한 뒤 실행하세요"
            )


def _shipper_org(db: Session, args: argparse.Namespace) -> OrganizationRow:
    query = select(OrganizationRow).where(
        OrganizationRow.type == "shipper", OrganizationRow.sandbox.is_(False)
    )
    if args.shipper_org_id:
        query = query.where(OrganizationRow.id == args.shipper_org_id)
    else:
        query = query.where(OrganizationRow.name == args.shipper_org_name)
    rows = list(db.scalars(query))
    if len(rows) != 1:
        sys.exit(f"화주 조직을 하나로 특정하지 못했습니다: {[row.id for row in rows]}")
    return rows[0]


def _synthetic(device_id: str, name: str | None) -> bool:
    return bool(SYNTHETIC_ID.match(device_id)) or any(m in (name or "") for m in SYNTHETIC_NAME)


def _occurrences(db: Session, needle: str) -> dict[str, int]:
    tables = db.scalars(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    ).all()
    found = {}
    for table in tables:
        count = db.execute(
            text(f'SELECT count(*) FROM "{table}" AS t WHERE t::text LIKE :pattern'),
            {"pattern": f"%{needle}%"},
        ).scalar()
        if count:
            found[table] = count
    return found


def _longer_token(db: Session, needle: str, tables: dict[str, int]) -> bool:
    for table in tables:
        hit = db.execute(
            text(f'SELECT 1 FROM "{table}" AS t WHERE t::text ~ :pattern LIMIT 1'),
            {"pattern": re.escape(needle) + "[A-Za-z0-9_]"},
        ).scalar()
        if hit:
            return True
    return False


def _new_number(db: Session, seed: str, taken: set[str]) -> str:
    attempt = 0
    while True:
        number = container_number(seed, attempt)
        if number not in taken and db.get(ForwarderResourceRow, number) is None:
            taken.add(number)
            return number
        attempt += 1


def _targets(db: Session, shipper: OrganizationRow) -> list[Target]:
    lot_ids = set(db.scalars(select(LotRow.id).where(LotRow.shipper_org_id == shipper.id)))
    targets: list[Target] = []
    taken: set[str] = set()
    for row in db.scalars(select(PlanRow).order_by(PlanRow.created_at)):
        session = PlanSession.model_validate(row.payload)
        containers = sorted(
            {
                a.container_id
                for a in session.plan.assignments
                if a.mode == "sea" and a.container_id and a.lot_id in lot_ids
            }
        )
        for container_id in containers:
            record = db.get(ContainerRow, f"{row.plan_id}:{container_id}")
            if record is None or record.org_id != row.org_id:
                continue
            device_ids = sorted(
                set(
                    db.scalars(
                        select(ReadingRow.device_id).where(
                            ReadingRow.record_key == record.record_key
                        )
                    )
                )
            )
            names = dict(
                db.execute(
                    select(DeviceRow.device_id, DeviceRow.name).where(
                        DeviceRow.device_id.in_(device_ids)
                    )
                ).all()
            )
            if not device_ids or not all(_synthetic(d, names.get(d)) for d in device_ids):
                continue
            bound = set(
                db.scalars(
                    select(BindingRow.device_id).where(
                        BindingRow.org_id == row.org_id,
                        BindingRow.plan_id == row.plan_id,
                        BindingRow.container_id == container_id,
                    )
                )
            )
            alerts = db.execute(
                text("SELECT count(*) FROM coldchain_alerts WHERE record_key = :key"),
                {"key": record.record_key},
            ).scalar()
            renamed = (
                container_id
                if is_iso6346(container_id)
                else _new_number(db, f"{row.plan_id}:{container_id}", taken)
            )
            targets.append(
                Target(
                    owner_org_id=row.org_id,
                    plan_id=row.plan_id,
                    old_container_id=container_id,
                    new_container_id=renamed,
                    record_key=record.record_key,
                    devices=sorted(set(device_ids) | bound),
                    readings=db.scalar(
                        select(func.count())
                        .select_from(ReadingRow)
                        .where(ReadingRow.record_key == record.record_key)
                    )
                    or 0,
                    alerts=alerts or 0,
                    session=session,
                    stage=record.stage,
                )
            )
    return targets


def _context(target: Target):
    return build_context(
        target.old_container_id,
        target.stage,
        ColdchainParams(),
        target.plan_id,
        target.session.scenario,
        target.session.plan,
    )


def _figures(readings: list[Reading], target: Target) -> dict:
    params = ColdchainParams()
    ctx = _context(target)
    ev = evaluate(readings, ctx, params)
    loss = loss_summary(ctx, ev.exposure, params)
    return {
        "band": f"{ctx.band_min_c:g}~{ctx.band_max_c:g}°C",
        "readings": len(readings),
        "span": f"{readings[0].ts.isoformat()} → {readings[-1].ts.isoformat()}" if readings else "",
        "status": ev.status,
        "temp_range": f"{min(r.temp_c for r in readings):.1f}~{max(r.temp_c for r in readings):.1f}°C"
        if readings
        else "",
        "outage_minutes": ev.outage.cumulative_minutes,
        "outage_episodes": ev.outage.episodes,
        "minutes_above_band": ev.excursion.minutes_above_band,
        "alerts": len(ev.alerts),
        "actions": sum(1 for a in ev.alerts if a.action is not None),
        "planned_loss_rate": loss.planned_loss_rate,
        "predicted_loss_rate": loss.predicted_loss_rate,
        "sensor_loss_rate": loss.sensor_loss_rate,
    }


def _current(db: Session, target: Target) -> dict:
    rows = db.scalars(
        select(ReadingRow.payload)
        .where(ReadingRow.record_key == target.record_key)
        .order_by(ReadingRow.ts)
    )
    return _figures([Reading.model_validate(p) for p in rows], target)


def _preview(target: Target, end: datetime) -> dict:
    ctx = _context(target)
    trace = reefer_trace(
        target.seed,
        device_id=reefer_device_id(target.seed),
        container_id=target.new_container_id,
        plan_id=target.plan_id,
        band_min_c=ctx.band_min_c,
        band_max_c=ctx.band_max_c,
        end=end,
    )
    readings = [Reading(**r.model_dump(exclude={"plan_id", "stage"})) for r in trace.readings]
    figures = _figures(readings, target)
    figures["setpoint_c"] = trace.setpoint_c
    figures["outage_window"] = f"{trace.outage.start.isoformat()} ({trace.outage.minutes}분)"
    figures["defrosts"] = len(trace.defrosts)
    return figures


def _report(shipper: OrganizationRow, targets: list[Target], end: datetime, db: Session) -> None:
    print(f"화주 조직: {shipper.id} ({shipper.name})")
    print(f"대상 컨테이너 기록: {len(targets)}건")
    for target in targets:
        print("-" * 72)
        print(f"계획 {target.plan_id} · 소유 조직 {target.owner_org_id}")
        vessel = next(
            (
                v.vessel_name
                for c in target.session.scenario.containers
                if c.id == target.old_container_id
                for v in target.session.scenario.voyages
                if v.id == c.voyage_id
            ),
            None,
        )
        print(f"  항차: {vessel}")
        print(f"  컨테이너: {target.old_container_id} → {target.new_container_id}")
        print(f"  삭제할 합성 기기: {', '.join(target.devices)}")
        print(f"  삭제할 측정값 {target.readings}건 · 알림 {target.alerts}건 · 바인딩·기기 행")
        print(f"  새 기기: {reefer_device_id(target.seed)}")
        print(f"  이름을 바꿀 행: {json.dumps(target.occurrences, ensure_ascii=False)}")
        print(f"  지금 값: {json.dumps(_current(db, target), ensure_ascii=False)}")
        print(f"  바꾼 뒤 값(미리보기): {json.dumps(_preview(target, end), ensure_ascii=False)}")


def _backup(args: argparse.Namespace, targets: list[Target], stamp: str) -> Path:
    folder = Path(args.backup_dir).expanduser() / f"coldchain-replace-{stamp}"
    folder.mkdir(parents=True, mode=0o700)
    env = dotenv_values(args.env_file)
    dump = folder / "tables.dump"
    command = ["exec", args.db_container, "pg_dump", "-U", env["POSTGRES_USER"], "-d"]
    command += [env["POSTGRES_DB"], "-Fc"]
    for table in BACKUP_TABLES:
        command += ["-t", table]
    with dump.open("wb") as handle:
        subprocess.run(["docker", *command], check=True, stdout=handle, timeout=300)
    os.chmod(dump, 0o600)
    manifest = folder / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": stamp,
                "tables": list(BACKUP_TABLES),
                "targets": [
                    {
                        "plan_id": t.plan_id,
                        "owner_org_id": t.owner_org_id,
                        "old_container_id": t.old_container_id,
                        "new_container_id": t.new_container_id,
                        "devices": t.devices,
                        "occurrences": t.occurrences,
                    }
                    for t in targets
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    os.chmod(manifest, 0o600)
    if dump.stat().st_size == 0:
        sys.exit("백업 파일이 비어 있어 중단합니다")
    return folder


def _rename(db: Session, target: Target) -> None:
    old, new = target.old_container_id, target.new_container_id
    if old == new:
        return
    columns = db.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND data_type = 'jsonb' AND table_name = ANY(:tables)"
        ),
        {"tables": list(RENAME_TABLES)},
    ).all()
    for table, column in columns:
        db.execute(
            text(
                f'UPDATE "{table}" SET "{column}" = replace("{column}"::text, :old, :new)::jsonb '
                f'WHERE "{column}"::text LIKE :pattern'
            ),
            {"old": old, "new": new, "pattern": f"%{old}%"},
        )
    db.execute(
        text("UPDATE forwarder_resources SET id = :new WHERE id = :old"), {"old": old, "new": new}
    )
    left = _occurrences(db, old)
    if left:
        raise RuntimeError(f"{old}가 아직 남아 있습니다: {left}")


def _erase(db: Session, target: Target) -> None:
    db.execute(delete(ContainerRow).where(ContainerRow.record_key == target.record_key))
    db.execute(
        delete(RunRow).where(
            RunRow.org_id == target.owner_org_id, RunRow.container_id == target.old_container_id
        )
    )
    db.execute(
        delete(BindingRow).where(
            or_(
                BindingRow.device_id.in_(target.devices),
                (BindingRow.org_id == target.owner_org_id)
                & (BindingRow.plan_id == target.plan_id)
                & (BindingRow.container_id == target.old_container_id),
            )
        )
    )
    db.execute(delete(DeviceRow).where(DeviceRow.device_id.in_(target.devices)))


def main() -> None:
    args = _args()
    end = datetime.now(UTC)
    engine = create_engine(_url(args, read_only=True))
    _check_schema(engine)
    with Session(engine) as db:
        shipper = _shipper_org(db, args)
        targets = _targets(db, shipper)
        for target in targets:
            target.occurrences = _occurrences(db, target.old_container_id)
            if _longer_token(db, target.old_container_id, target.occurrences):
                sys.exit(f"{target.old_container_id}로 시작하는 더 긴 값이 있어 중단합니다")
        _report(shipper, targets, end, db)
    if not targets:
        print("바꿀 합성 기록이 없습니다")
        return
    if not args.apply:
        print("-" * 72)
        print("dry-run: 아무것도 쓰지 않았습니다. 실제로 바꾸려면 --apply")
        return
    if not args.skip_api_check:
        _check_api(args)
    stamp = end.strftime("%Y%m%d-%H%M%S")
    folder = _backup(args, targets, stamp)
    print(f"백업: {folder}")
    database.configure(_url(args, read_only=False).render_as_string(hide_password=False))
    with database.begin() as db:
        for target in targets:
            _erase(db, target)
            _rename(db, target)
    for target in targets:
        seeded = seed_container(
            coldchain_store,
            org_id=target.owner_org_id,
            plan_id=target.plan_id,
            container_id=target.new_container_id,
            seed=target.seed,
            end=end,
            stage=target.stage,
        )
        if seeded is None:
            sys.exit(
                f"{target.new_container_id}: 같은 기기 id가 이미 있어 측정값을 넣지 못했습니다"
            )
        state = coldchain_store.state(
            target.new_container_id, target.plan_id, 0, target.owner_org_id
        )
        print(
            f"{target.new_container_id}: 기기 {seeded.device_id} · 측정값 {seeded.readings}건 · "
            f"상태 {state.status} · 누적 단전 {state.outage.cumulative_minutes:g}분 · "
            f"적정 초과 {state.excursion.minutes_above_band:g}분 · 알림 {len(state.alerts)}건"
        )


if __name__ == "__main__":
    main()
