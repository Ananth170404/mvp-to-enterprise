"""Indian GST identification number (GSTIN) validation.

Format: 2-digit state code + 10-char PAN + entity number + 'Z' + check character.
The check character is a mod-36 checksum over the first 14 characters, so a
mis-read digit from OCR or an LLM is usually caught.
"""

import re

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        value = _CHARS.index(ch)
        product = value * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return _CHARS[(36 - total % 36) % 36]


def is_valid(gstin: str | None) -> bool:
    if not gstin:
        return False
    gstin = gstin.strip().upper()
    if not _PATTERN.match(gstin):
        return False
    return checksum_char(gstin[:14]) == gstin[14]


def state_code(gstin: str) -> str:
    return gstin[:2]
