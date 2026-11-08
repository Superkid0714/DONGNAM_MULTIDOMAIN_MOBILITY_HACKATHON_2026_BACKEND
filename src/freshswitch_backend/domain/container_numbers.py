import hashlib
import re
import string

REEFER_OWNER_CODES = ("SZLU", "TRIU", "CXRU")
SERIAL_SPACE = 1_000_000
_ISO_NUMBER = re.compile(r"^[A-Z]{3}[UJZ]\d{7}$")
_LETTER_VALUES = dict(
    zip(
        string.ascii_uppercase,
        [n for n in range(10, 39) if n % 11 != 0],
        strict=True,
    )
)


def _value(char: str) -> int:
    return int(char) if char.isdigit() else _LETTER_VALUES[char]


def iso6346_check_digit(code: str) -> int:
    if len(code) != 10:
        raise ValueError(f"소유자 코드·일련번호 10자리가 필요합니다: {code}")
    total = sum(_value(char) * 2**index for index, char in enumerate(code))
    return total % 11 % 10


def is_iso6346(number: str) -> bool:
    if _ISO_NUMBER.fullmatch(number) is None:
        return False
    return iso6346_check_digit(number[:10]) == int(number[10])


def container_number(seed: str, attempt: int = 0) -> str:
    digest = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big")
    owner = REEFER_OWNER_CODES[digest % len(REEFER_OWNER_CODES)]
    serial = (digest // len(REEFER_OWNER_CODES) + attempt * 7919) % SERIAL_SPACE
    code = f"{owner}{serial:06d}"
    return f"{code}{iso6346_check_digit(code)}"


def printed_container_number(number: str) -> str:
    return f"{number[:4]} {number[4:10]} {number[10:]}"
