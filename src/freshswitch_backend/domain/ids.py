import re
from typing import Annotated

from pydantic import AfterValidator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
ID_MAX_LENGTH = 64
ID_RULE = (
    f"id는 영문·숫자로 시작하고 영문·숫자·'.'·'_'·'-'만 쓸 수 있으며 {ID_MAX_LENGTH}자 이하입니다"
)

_ID = re.compile(ID_PATTERN)


def id_error(value: str) -> str | None:
    if len(value) > ID_MAX_LENGTH or _ID.fullmatch(value) is None:
        return ID_RULE
    return None


def check_id(value: str) -> str:
    error = id_error(value)
    if error is not None:
        raise ValueError(error)
    return value


EntityId = Annotated[str, AfterValidator(check_id)]
