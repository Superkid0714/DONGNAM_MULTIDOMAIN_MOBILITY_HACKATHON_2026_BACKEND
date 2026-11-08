from typing import Annotated

from pydantic import BeforeValidator, Field

UNSTORABLE_TEXT = "NUL 문자나 인코딩할 수 없는 문자는 사용할 수 없습니다"
NOTE_MAX_LENGTH = 2000


def storable(value: object) -> object:
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError(UNSTORABLE_TEXT)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(UNSTORABLE_TEXT) from exc
    return value


StorableStr = Annotated[str, BeforeValidator(storable)]
Note = Annotated[StorableStr, Field(max_length=NOTE_MAX_LENGTH)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
