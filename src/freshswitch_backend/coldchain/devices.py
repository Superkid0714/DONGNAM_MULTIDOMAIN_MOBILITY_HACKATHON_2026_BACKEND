import hashlib
import re
import secrets
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from freshswitch_backend.coldchain.models import Binding
from freshswitch_backend.domain.ids import check_id
from freshswitch_backend.domain.inputs import StorableStr
from freshswitch_backend.persistence import Database, database
from freshswitch_backend.persistence.tables import BindingRow, DeviceRow

SIMULATOR_PREFIX = "sim-"
SANDBOX_DEVICE_PREFIX = "demo-"
DEVICE_ID_MAX = 64
DEVICE_SLUG_MAX = 50
TOO_LONG = "기기 id가 너무 깁니다"
OTHER_WORLD_DEVICE = "다른 데모 세계의 기기 id입니다"
LIVE_WORLD_DEVICE = "데모 세계 기기 id는 등록할 수 없습니다"
_VISITOR_WORLD = re.compile(r"^w[0-9a-f]{12}$")
_OTHER_SIM = re.compile(r"^sim-([0-9a-f]{12})-")
_OTHER_DEVICE = re.compile(r"^d([0-9a-f]{12})-")
_LIVE_WORLD_DEVICE = re.compile(r"^d[0-9a-f]{12}-", re.IGNORECASE)


class DeviceConflict(ValueError):
    pass


class DeviceIdError(ValueError):
    pass


def is_simulator_device(device_id: str) -> bool:
    return device_id.startswith(SIMULATOR_PREFIX)


def is_visitor_world(world_id: str | None) -> bool:
    return bool(world_id and _VISITOR_WORLD.fullmatch(world_id))


def own_simulator(device_id: str, world_id: str | None) -> bool:
    return is_visitor_world(world_id) and device_id.startswith(f"sim-{world_id[1:]}-")


def _limit(device_id: str) -> str:
    if len(device_id) > DEVICE_ID_MAX:
        raise DeviceIdError(TOO_LONG)
    return device_id


def device_prefix_error(device_id: str, sandbox: bool) -> str | None:
    lowered = device_id.lower()
    if sandbox and not lowered.startswith(SANDBOX_DEVICE_PREFIX):
        return f"데모 조직 기기 id는 '{SANDBOX_DEVICE_PREFIX}'로 시작해야 합니다"
    if not sandbox and lowered.startswith(SANDBOX_DEVICE_PREFIX):
        return f"'{SANDBOX_DEVICE_PREFIX}'로 시작하는 id는 데모 조직용이라 등록할 수 없습니다"
    if not sandbox and lowered.startswith(SIMULATOR_PREFIX):
        return f"'{SIMULATOR_PREFIX}'로 시작하는 id는 시뮬레이터용이라 등록할 수 없습니다"
    if not sandbox and _LIVE_WORLD_DEVICE.match(device_id):
        return LIVE_WORLD_DEVICE
    return None


def rewrite_simulator_id(device_id: str, world_id: str | None) -> str:
    if not is_visitor_world(world_id):
        return device_id
    token = world_id[1:]
    own = f"sim-{token}-"
    if device_id.startswith(own):
        return _limit(device_id)
    matched = _OTHER_SIM.match(device_id)
    if matched is not None and matched.group(1) != token:
        raise DeviceIdError(OTHER_WORLD_DEVICE)
    if device_id.startswith(SIMULATOR_PREFIX):
        rest = device_id[len(SIMULATOR_PREFIX) :]
        return _limit(f"sim-{token}-{rest}")
    return device_id


def rewrite_registered_id(device_id: str, world_id: str | None) -> str:
    if not is_visitor_world(world_id):
        return device_id
    token = world_id[1:]
    own = f"d{token}-"
    if device_id.startswith(own):
        return _limit(device_id)
    sim = _OTHER_SIM.match(device_id)
    dev = _OTHER_DEVICE.match(device_id)
    if (sim is not None and sim.group(1) != token) or (
        dev is not None and dev.group(1) != token
    ):
        raise DeviceIdError(OTHER_WORLD_DEVICE)
    slug = device_id
    if slug.lower().startswith(SANDBOX_DEVICE_PREFIX):
        slug = slug[len(SANDBOX_DEVICE_PREFIX) :]
    if len(slug) > DEVICE_SLUG_MAX:
        raise DeviceIdError(TOO_LONG)
    return _limit(f"d{token}-{slug}")


class DeviceRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=100)
    name: StorableStr | None = Field(default=None, max_length=200)

    @field_validator("device_id")
    @classmethod
    def _device_id(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("빈 값은 사용할 수 없습니다")
        if is_simulator_device(trimmed):
            raise ValueError(
                f"'{SIMULATOR_PREFIX}'로 시작하는 id는 시뮬레이터용이라 등록할 수 없습니다"
            )
        return check_id(trimmed)


class Device(BaseModel):
    device_id: str
    name: str | None = None
    created_at: datetime
    last_seen_at: datetime | None = None
    binding: Binding | None = None


class RegisteredDevice(Device):
    device_token: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


class DeviceStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db or database
        self.clock: Callable[[], datetime] = _now

    def _device(self, row: DeviceRow, binding: BindingRow | None) -> Device:
        return Device(
            device_id=row.device_id,
            name=row.name,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            binding=Binding.model_validate(binding.payload) if binding is not None else None,
        )

    def register(self, body: DeviceRequest, org_id: str) -> RegisteredDevice:
        token = secrets.token_urlsafe(32)
        try:
            with self._db.begin() as db:
                if db.get(DeviceRow, body.device_id) is not None:
                    raise DeviceConflict(f"이미 등록된 장치입니다: {body.device_id}")
                binding = db.get(BindingRow, body.device_id)
                if binding is not None and binding.org_id != org_id:
                    raise DeviceConflict(f"다른 조직에 연결된 장치입니다: {body.device_id}")
                row = DeviceRow(
                    device_id=body.device_id,
                    org_id=org_id,
                    token_hash=token_hash(token),
                    name=body.name,
                    created_at=self.clock(),
                )
                db.add(row)
                db.flush()
                device = self._device(row, db.get(BindingRow, body.device_id))
        except IntegrityError as exc:
            raise DeviceConflict(f"이미 등록된 장치입니다: {body.device_id}") from exc
        return RegisteredDevice(**device.model_dump(), device_token=token)

    def for_org(self, org_id: str) -> list[Device]:
        with self._db.begin() as db:
            rows = db.execute(
                select(DeviceRow, BindingRow)
                .outerjoin(
                    BindingRow,
                    (BindingRow.device_id == DeviceRow.device_id)
                    & (BindingRow.org_id == DeviceRow.org_id),
                )
                .where(DeviceRow.org_id == org_id)
                .order_by(DeviceRow.device_id)
            ).all()
            return [self._device(row, binding) for row, binding in rows]

    def delete(self, device_id: str, org_id: str) -> bool:
        with self._db.begin() as db:
            row = db.get(DeviceRow, device_id)
            if row is None or row.org_id != org_id:
                return False
            db.execute(
                delete(BindingRow).where(
                    BindingRow.device_id == device_id, BindingRow.org_id == org_id
                )
            )
            db.delete(row)
            return True

    def owners(self, device_ids: Iterable[str]) -> dict[str, str]:
        ids = set(device_ids)
        if not ids:
            return {}
        with self._db.begin() as db:
            rows = db.execute(
                select(DeviceRow.device_id, DeviceRow.org_id).where(DeviceRow.device_id.in_(ids))
            ).all()
            return dict(rows)

    def verify(self, device_id: str, token: str | None) -> bool:
        if not token:
            return False
        with self._db.begin() as db:
            stored = db.scalar(select(DeviceRow.token_hash).where(DeviceRow.device_id == device_id))
        if stored is None:
            return False
        return secrets.compare_digest(token_hash(token).encode("ascii"), stored.encode("ascii"))

    def touch(self, device_ids: Iterable[str]) -> None:
        ids = set(device_ids)
        if not ids:
            return
        with self._db.begin() as db:
            db.execute(
                update(DeviceRow)
                .where(DeviceRow.device_id.in_(ids))
                .values(last_seen_at=self.clock())
            )


device_store = DeviceStore()
