"""Control messages must match scrcpy's own serialization test vectors (app/tests/test_control_msg_serialize.c)."""

from mobile_rpa import scrcpy


def test_touch_matches_scrcpy_vector_layout():
    msg = scrcpy.pack_touch(0, 100, 200, 1080, 1920, pointer=0x1234567887654321)
    assert len(msg) == 32
    assert msg[:22] == bytes([
        2, 0,
        0x12, 0x34, 0x56, 0x78, 0x87, 0x65, 0x43, 0x21,
        0, 0, 0, 0x64, 0, 0, 0, 0xC8,
        0x04, 0x38, 0x07, 0x80,
    ])
    assert msg[22:24] == b"\xff\xff"  # full pressure on down


def test_touch_up_has_zero_pressure():
    assert scrcpy.pack_touch(1, 1, 1, 10, 10)[22:24] == b"\x00\x00"


def test_keycode_vector():
    msg = scrcpy.pack_key(scrcpy.KEY_UP, 66, repeat=5, meta=0x41)
    assert msg == bytes([0, 1, 0, 0, 0, 0x42, 0, 0, 0, 5, 0, 0, 0, 0x41])


def test_text_vector():
    assert scrcpy.pack_text("hello, world!") == bytes([1, 0, 0, 0, 13]) + b"hello, world!"


def test_scroll_vector():
    msg = scrcpy.pack_scroll(260, 1026, 1080, 1920, 16, -16)
    assert msg[:17] == bytes([3, 0, 0, 1, 4, 0, 0, 4, 2, 4, 0x38, 7, 0x80, 0x7F, 0xFF, 0x80, 0x00])
    assert len(msg) == 21


def test_codec_string_from_sps():
    config = bytes.fromhex("0000000167 42c029 8d680b".replace(" ", ""))
    assert scrcpy.codec_string(config) == "avc1.42C029"
    assert scrcpy.codec_string(b"junk") == "avc1.42E01F"


def test_late_viewer_waits_for_fresh_keyframe():
    session = scrcpy.ScrcpySession.__new__(scrcpy.ScrcpySession)
    session.viewers, session.config, session.keyframe = set(), b"\x00\x00\x00\x01\x67\x42\xc0\x29", b"old"
    session.width, session.height, session.quality, session.codec = 540, 960, "low", "h264"
    viewer = session.attach()
    kinds = [viewer.queue.get_nowait()[0] for _ in range(viewer.queue.qsize())]
    assert kinds == ["meta", "frame"]  # config only, never the stale keyframe
    assert viewer.needs_key
    session._broadcast(0, b"delta")
    assert viewer.queue.empty()
    session._broadcast(scrcpy.FLAG_KEY, b"key")
    assert viewer.queue.get_nowait() == ("frame", bytes([scrcpy.FLAG_KEY]) + b"key")
    assert not viewer.needs_key



def test_hevc_codec_string_from_sps():
    # Annex-B SPS: start code, NAL header 42 01, sps ids 01, PTL: Main profile (0x01),
    # compat flags 0x60000000, 6 constraint bytes, level 90 (0x5A)
    sps = bytes.fromhex("00000001" "4201" "01" "01" "60000000" "900000000000" "5a")
    assert scrcpy.codec_string(sps + b"\x00" * 20, "h265") == "hvc1.1.6.L90.B0"
    assert scrcpy.codec_string(b"junk", "h265") == "hvc1.1.6.L93.B0"
