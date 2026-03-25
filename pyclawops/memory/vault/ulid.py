"""Pure-Python ULID generator.

ULIDs are 128-bit identifiers encoded as 26-character Crockford base32 strings:
- 48 bits: millisecond timestamp
- 80 bits: cryptographically random

They sort chronologically as strings, which makes them ideal for ordered IDs.
"""

import os
import time
from datetime import datetime, timezone

# Crockford Base32 alphabet (excludes I, L, O, U to avoid confusion)
_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODING = {ch: i for i, ch in enumerate(_ENCODING)}


def generate() -> str:
    """Generate a new ULID string (26 uppercase Crockford base32 chars).

    Returns:
        str: A 26-character ULID string that sorts chronologically.
    """
    # 48-bit millisecond timestamp
    ts_ms = int(time.time() * 1000)
    # 80-bit random
    rand = int.from_bytes(os.urandom(10), "big")

    # Encode timestamp (10 chars, 5 bits each = 50 bits, but we only use 48)
    ts_chars = []
    t = ts_ms
    for _ in range(10):
        ts_chars.append(_ENCODING[t & 0x1F])
        t >>= 5
    ts_chars.reverse()

    # Encode random (16 chars, 5 bits each = 80 bits)
    rand_chars = []
    r = rand
    for _ in range(16):
        rand_chars.append(_ENCODING[r & 0x1F])
        r >>= 5
    rand_chars.reverse()

    return "".join(ts_chars) + "".join(rand_chars)


def timestamp(ulid_str: str) -> datetime:
    """Extract the timestamp embedded in a ULID string.

    Args:
        ulid_str: A 26-character ULID string.

    Returns:
        datetime: UTC datetime corresponding to the ULID's embedded timestamp.

    Raises:
        ValueError: If the ULID string is not valid (wrong length or chars).
    """
    if len(ulid_str) != 26:
        raise ValueError(f"ULID must be 26 characters, got {len(ulid_str)}")

    ulid_upper = ulid_str.upper()
    try:
        ts_ms = 0
        for ch in ulid_upper[:10]:
            ts_ms = (ts_ms << 5) | _DECODING[ch]
    except KeyError as exc:
        raise ValueError(f"Invalid ULID character: {exc}") from exc

    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
