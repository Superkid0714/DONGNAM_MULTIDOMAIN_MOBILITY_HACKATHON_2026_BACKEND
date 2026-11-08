from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from freshswitch_backend.api.intake import intake_error, register
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.intake.models import FieldError, IntakePreview, IntakeRequest
from freshswitch_backend.intake.service import IntakeError, resolve_names
from freshswitch_backend.lots import LotFilter, LotPatch, LotStatus, RegisteredLot, lot_store

router = APIRouter(prefix="/lots", tags=["lots"])


def _lot(lot_id: str) -> RegisteredLot:
    lot = lot_store.get(lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="등록된 로트를 찾을 수 없습니다")
    return lot


@router.post("", status_code=status.HTTP_201_CREATED)
def create_lot(body: IntakeRequest) -> IntakePreview:
    return register(body)


@router.get("")
def list_lots(
    scenario_id: str | None = None,
    shipper: str | None = None,
    product: str | None = None,
    destination: str | None = None,
    status: LotStatus | None = None,
    ready_from: date | None = None,
    ready_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RegisteredLot]:
    return lot_store.search(
        LotFilter(
            scenario_id=scenario_id,
            shipper=shipper,
            product=product,
            destination=destination,
            status=status,
            ready_from=ready_from,
            ready_to=ready_to,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/{lot_id}")
def read_lot(lot_id: str) -> RegisteredLot:
    return _lot(lot_id)


def _validate_patch(lot: RegisteredLot, patch: LotPatch) -> dict[str, Any]:
    changes = patch.model_dump(exclude_unset=True)
    values: dict[str, Any] = {}
    errors: list[FieldError] = []
    scenario = get_scenario(lot.scenario_id)
    if {"product", "shipper", "destination"} & changes.keys():
        if scenario is None:
            raise HTTPException(
                status_code=422, detail="로트의 시나리오가 없어 이름을 다시 찾을 수 없습니다"
            )
        resolved = resolve_names(
            scenario,
            IntakeRequest(
                product=changes.get("product") or lot.product_id,
                shipper=changes.get("shipper") or lot.shipper_id,
                destination=changes.get("destination") or lot.destination,
                pallets=lot.pallets,
                ready_date=lot.ready_date,
            ),
        )
        values.update(
            product_id=resolved.product_id,
            shipper_id=resolved.shipper_id,
            packhouse=resolved.packhouse_id,
            destination=resolved.destination_port_id,
        )
    for name in ("ready_date", "sell_by_date", "sell_window", "status", "pallets"):
        if changes.get(name) is not None:
            values[name] = changes[name]
    if "pallets" in values:
        capacity = (
            max((c.capacity_pallets for c in scenario.containers), default=20) if scenario else 20
        )
        if values["pallets"] <= 0:
            errors.append(FieldError(field="pallets", message="파렛트 수는 1 이상이어야 합니다"))
        elif values["pallets"] > capacity:
            errors.append(
                FieldError(
                    field="pallets",
                    message=f"컨테이너 1대 용량({capacity}파렛트)을 넘습니다. 나눠서 등록하세요",
                )
            )
    ready = values.get("ready_date", lot.ready_date)
    sell_by = values.get("sell_by_date", lot.sell_by_date)
    if sell_by < ready:
        errors.append(
            FieldError(field="sell_by_date", message="판매기한이 출하 가능일보다 빠릅니다")
        )
    if errors:
        raise IntakeError(errors)
    return values


@router.patch("/{lot_id}")
def update_lot(lot_id: str, patch: LotPatch) -> RegisteredLot:
    try:
        updated = lot_store.update(lot_id, lambda lot: _validate_patch(lot, patch))
    except IntakeError as exc:
        raise intake_error(exc) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="등록된 로트를 찾을 수 없습니다")
    return updated


@router.delete("/{lot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lot(lot_id: str) -> None:
    if not lot_store.delete(lot_id):
        raise HTTPException(status_code=404, detail="등록된 로트를 찾을 수 없습니다")
