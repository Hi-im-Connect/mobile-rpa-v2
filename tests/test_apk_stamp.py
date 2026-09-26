import io
import struct
import zipfile

import pytest

from mobile_rpa.apk_stamp import MAGIC, StampError, read_stamp, stamp


def fake_apk() -> bytes:
    """A zip with an APK Signing Block (one dummy 'signature' pair) between the entries and the central directory."""
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        z.writestr("AndroidManifest.xml", "hello")
        z.writestr("classes.dex", "x" * 100)
    data = raw.getvalue()
    eocd = data.rindex(b"PK\x05\x06")
    cd_offset = struct.unpack_from("<I", data, eocd + 16)[0]
    pair = struct.pack("<QI", 4 + 3, 0x7109871A) + b"sig"
    size = len(pair) + 8 + 16
    block = struct.pack("<Q", size) + pair + struct.pack("<Q", size) + MAGIC
    out = bytearray(data[:cd_offset] + block + data[cd_offset:])
    struct.pack_into("<I", out, eocd + len(block) + 16, cd_offset + len(block))
    return bytes(out)


def test_stamp_round_trip_keeps_the_zip_and_the_other_pairs():
    apk = fake_apk()
    stamped = stamp(apk, b'{"token":"abc"}')
    assert read_stamp(stamped) == b'{"token":"abc"}'
    assert read_stamp(apk) is None
    with zipfile.ZipFile(io.BytesIO(stamped)) as z:
        assert z.read("AndroidManifest.xml") == b"hello" and z.testzip() is None
    assert b"sig" in stamped
    # the signing block starts where it did, so the v2 signature's view of the file is unchanged
    assert stamped[: apk.index(MAGIC) - 8 - 23] == apk[: apk.index(MAGIC) - 8 - 23]


def test_restamping_replaces_the_old_value():
    once = stamp(fake_apk(), b"one")
    twice = stamp(once, b"two")
    assert read_stamp(twice) == b"two" and b"one" not in twice


def test_a_plain_zip_is_refused():
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        z.writestr("a", "b")
    with pytest.raises(StampError):
        stamp(raw.getvalue(), b"x")
