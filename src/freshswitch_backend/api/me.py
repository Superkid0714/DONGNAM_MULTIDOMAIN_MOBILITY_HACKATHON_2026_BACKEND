from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from freshswitch_backend.api.auth import AuthPrincipal, Me, MeUser, load_me
from freshswitch_backend.domain.inputs import storable
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.sandbox_world import SEED_ID
from freshswitch_backend.persistence.tables import UserRow

router = APIRouter(tags=["auth"])


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=200)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: object) -> object:
        value = storable(value)
        return value.strip() if isinstance(value, str) else value


@router.get("/me")
def read_me(principal: AuthPrincipal) -> Me:
    return load_me(principal)


@router.patch("/me")
def update_me(body: ProfileUpdate, principal: AuthPrincipal) -> MeUser:
    with database.begin() as db:
        user = db.get(UserRow, principal.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="로그인이 필요합니다")
        if user.is_demo and user.world_id in {None, SEED_ID}:
            raise HTTPException(
                status_code=403, detail="공용 데모 계정의 이름은 수정할 수 없습니다"
            )
        user.name = body.name
        return MeUser(id=user.id, email=user.email, name=user.name)
