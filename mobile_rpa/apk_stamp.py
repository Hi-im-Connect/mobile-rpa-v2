"""Put an invite into a downloaded APK without re-signing it (the Walle method).

An APK signed with scheme v2 has an "APK Signing Block" between its zip entries and the central
directory. The signature covers everything except that block's contents, so an extra ID-value
pair can be added there: the signature stays valid and Android installs it as usual. The app
reads the pair from its own APK on first launch (agent/InstallStamp.kt) and connects by itself.

Layout of the block: size (u64) | pairs: [length (u64) | id (u32) | value] ... | size (u64) | magic.
"""

from __future__ import annotations

import struct

MAGIC = b"APK Sig Block 42"
STAMP_ID = 0x46414155  # "FAAU"; must match InstallStamp.kt
_EOCD = b"PK\x05\x06"


class StampError(ValueError):
    pass


def _layout(apk: bytes) -> tuple[int, int, int]:
    """(end of central directory, central directory offset, signing block start)."""
    eocd = apk.rfind(_EOCD, max(0, len(apk) - 65_557))
    if eocd < 0:
        raise StampError("not a zip file")
    cd = struct.unpack_from("<I", apk, eocd + 16)[0]
    if cd < 24 or apk[cd - 16:cd] != MAGIC:
        raise StampError("no APK Signing Block (the APK must be signed with scheme v2)")
    size = struct.unpack_from("<Q", apk, cd - 24)[0]
    start = cd - size - 8
    if start < 0 or struct.unpack_from("<Q", apk, start)[0] != size:
        raise StampError("damaged APK Signing Block")
    return eocd, cd, start


def _pairs(apk: bytes, start: int, cd: int) -> list[tuple[int, bytes]]:
    pairs, pos, end = [], start + 8, cd - 24
    while pos < end:
        length = struct.unpack_from("<Q", apk, pos)[0]
        pair_id = struct.unpack_from("<I", apk, pos + 8)[0]
        pairs.append((pair_id, apk[pos + 12:pos + 8 + length]))
        pos += 8 + length
    return pairs


def stamp(apk: bytes, value: bytes) -> bytes:
    """The same APK with `value` stored under STAMP_ID (replacing any earlier stamp)."""
    eocd, cd, start = _layout(apk)
    pairs = [p for p in _pairs(apk, start, cd) if p[0] != STAMP_ID] + [(STAMP_ID, value)]
    body = b"".join(struct.pack("<QI", len(v) + 4, i) + v for i, v in pairs)
    size = len(body) + 8 + 16
    block = struct.pack("<Q", size) + body + struct.pack("<Q", size) + MAGIC
    tail = bytearray(apk[cd:])
    new_cd = start + len(block)
    struct.pack_into("<I", tail, eocd - cd + 16, new_cd)
    return apk[:start] + block + bytes(tail)


def read_stamp(apk: bytes) -> bytes | None:
    _, cd, start = _layout(apk)
    return next((v for i, v in _pairs(apk, start, cd) if i == STAMP_ID), None)
