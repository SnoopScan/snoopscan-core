"""Prefixed ULID identifiers (02-data-model.md conventions).

Sortable by creation time, readable in logs, no coordination needed. Written by
hand rather than pulled from a dependency — it is 40 lines and avoids adding a
package to the licence surface for something this small.
"""

from __future__ import annotations

import os
import time

# Crockford base32: no I, L, O or U, so ids stay unambiguous when read aloud
# or transcribed from a log.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODED_TIME_LENGTH = 10
_ENCODED_RANDOM_LENGTH = 16


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid() -> str:
    """A ULID: 48-bit millisecond timestamp then 80 bits of randomness."""
    timestamp_ms = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(timestamp_ms, _ENCODED_TIME_LENGTH) + _encode(randomness, _ENCODED_RANDOM_LENGTH)


def new_id(prefix: str) -> str:
    """A prefixed id, e.g. `crawl_01J8Z...`."""
    return f"{prefix}_{ulid()}"


def job_id(kind: str) -> str:
    return new_id(kind)


def page_id() -> str:
    return new_id("page")


def timestamp_of(identifier: str) -> float:
    """Creation time in epoch seconds, decoded from the id itself."""
    body = identifier.split("_", 1)[-1]
    encoded = body[:_ENCODED_TIME_LENGTH]
    value = 0
    for char in encoded:
        index = _ALPHABET.find(char.upper())
        if index < 0:
            raise ValueError(f"not a valid ULID character: {char!r}")
        value = (value << 5) | index
    return value / 1000
