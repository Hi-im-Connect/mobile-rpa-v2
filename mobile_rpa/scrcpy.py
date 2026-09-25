"""Live H.264 screen + remote input through the scrcpy server (protocol of scrcpy 4.1).

One ScrcpySession per watched phone. It fans out every video packet to all viewers and keeps
the codec config and the latest keyframe, so a viewer who joins late starts decoding at once.
Viewers that fall behind skip ahead to the next keyframe instead of buffering forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import socket
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from .adb import Adb

log = logging.getLogger("mobile_rpa.scrcpy")

SERVER_VERSION = "4.1"
SERVER_JAR = Path(__file__).with_name("scrcpy-server-v4.1.jar")
DEVICE_JAR = "/data/local/tmp/mobile-rpa-scrcpy.jar"
IDLE_STOP_SECONDS = 60.0
VIEWER_QUEUE = 30  # ~1s at 30fps: a slow viewer skips to the next keyframe instead of lagging

# quality -> (max_size = LONGEST side px, bitrate bps, fps, constant bitrate).
# Measured on a relayed NetBird link (docs/research/2026-09-25-slow-link-streaming.md):
# VBR overshoots 25-65% under motion, CBR holds the target; 960 is the floor for readable text
# on a 20:9 phone (720 would be only 324 px wide).
# One compressed profile everywhere (live view, thumbnails' source, agent screenshots).
QUALITY = {
    "low": (960, 800_000, 15, True),  # 432x960 on a 20:9 phone, ~0.75 Mbit/s h264, ~0.6 h265
}
THUMB_PROFILE = (480, 300_000, 5, True)
PREVIEW_IDLE_S = 30  # close a card's preview stream this long after the last thumbnail request
BITRATE_MODE_CBR = 2  # MediaFormat.BITRATE_MODE_CBR

FLAG_CONFIG = 1
FLAG_KEY = 2

# control message types (scrcpy ControlMessage.java)
TYPE_KEYCODE, TYPE_TEXT, TYPE_TOUCH, TYPE_SCROLL, TYPE_BACK = 0, 1, 2, 3, 4
TYPE_RESET_VIDEO = 17
POINTER_FINGER = -2
KEY_DOWN, KEY_UP = 0, 1


# ---- control message packing --------------------------------------------------------------
def pack_touch(action: int, x: int, y: int, width: int, height: int, pointer: int = POINTER_FINGER) -> bytes:
    pressure = 0 if action == 1 else 0xFFFF
    return struct.pack(">BBqiiHHHII", TYPE_TOUCH, action, pointer, x, y, width, height, pressure, 0, 0)


def pack_key(action: int, keycode: int, repeat: int = 0, meta: int = 0) -> bytes:
    return struct.pack(">BBIII", TYPE_KEYCODE, action, keycode, repeat, meta)


def pack_text(text: str) -> bytes:
    data = text.encode()[:300]
    return struct.pack(">BI", TYPE_TEXT, len(data)) + data


def _scroll_fixed(value: float) -> int:
    """scrcpy encodes scroll amounts in [-16, 16] as a signed 16-bit fixed point."""
    clamped = max(-1.0, min(1.0, value / 16))
    return 0x7FFF if clamped >= 1 else int(clamped * 0x8000) & 0xFFFF


def pack_scroll(x: int, y: int, width: int, height: int, h: float, v: float) -> bytes:
    return struct.pack(
        ">BiiHHHHI", TYPE_SCROLL, x, y, width, height, _scroll_fixed(h), _scroll_fixed(v), 0
    )


def _unescape(nal: bytes) -> bytes:
    """Drop H.264/H.265 emulation-prevention bytes (00 00 03 -> 00 00)."""
    return nal.replace(b"\x00\x00\x03", b"\x00\x00")


def hevc_codec_string(config_packet: bytes) -> str:
    """hvc1.P.C.TL.B0 from the H.265 SPS (NAL type 33) of an Annex-B config packet."""
    idx = config_packet.find(b"\x00\x00\x01\x42")
    if idx < 0:
        return "hvc1.1.6.L93.B0"
    sps = _unescape(config_packet[idx + 3 : idx + 40])
    if len(sps) < 15:
        return "hvc1.1.6.L93.B0"
    ptl = sps[3:]  # after the 2-byte NAL header and 1 byte of SPS ids
    profile_space, tier, profile = ptl[0] >> 6, (ptl[0] >> 5) & 1, ptl[0] & 0x1F
    compat = int.from_bytes(ptl[1:5], "big")
    compat_rev = int(f"{compat:032b}"[::-1], 2)  # the codec string lists the flags bit-reversed
    level = ptl[11]
    space = "ABC"[profile_space - 1] if profile_space else ""
    return f"hvc1.{space}{profile}.{compat_rev:X}.{'H' if tier else 'L'}{level}.B0"


def codec_string(config_packet: bytes, codec: str = "h264") -> str:
    """avc1.PPCCLL (or the hvc1 form) from the SPS inside an Annex-B config packet."""
    if codec == "h265":
        return hevc_codec_string(config_packet)
    idx = config_packet.find(b"\x00\x00\x01\x67")
    if idx < 0 or idx + 7 > len(config_packet):
        return "avc1.42E01F"
    sps = config_packet[idx + 4 : idx + 7]
    return "avc1." + sps.hex().upper()


# ---- session --------------------------------------------------------------------------------
@dataclass(eq=False)
class Viewer:
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(VIEWER_QUEUE))
    needs_key: bool = False


class ScrcpySession:
    def __init__(
        self, adb: Adb, serial: str, quality: str = "medium", codec: str = "h264",
        profile: tuple | None = None,
    ) -> None:
        self.adb, self.serial = adb, serial
        self.quality = "low"
        self.codec = codec if codec in ("h264", "h265") else "h264"
        self.max_size, self.bitrate, self.fps, self.cbr = profile or QUALITY[self.quality]
        self._key_waiters: list[asyncio.Future] = []
        self.width = self.height = 0
        self.config: bytes = b""
        self.keyframe: bytes = b""
        self.viewers: set[Viewer] = set()
        self.closed = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._port = 0
        self._video: asyncio.StreamReader | None = None
        self._control: asyncio.StreamWriter | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._tasks: list[asyncio.Task] = []
        self._control_lock = asyncio.Lock()

    @property
    def meta(self) -> dict:
        return {
            "type": "meta",
            "quality": self.quality,
            "width": self.width,
            "height": self.height,
            "codec": codec_string(self.config, self.codec) if self.config else "",
        }

    async def start(self) -> None:
        await self._ensure_jar()
        scid = random.randint(0, 0x7FFFFFFF)
        self._port = _free_port()
        await self.adb.run(
            "forward", f"tcp:{self._port}", f"localabstract:scrcpy_{scid:08x}", serial=self.serial
        )
        args = (
            f"CLASSPATH={DEVICE_JAR} app_process / com.genymobile.scrcpy.Server {SERVER_VERSION} "
            f"scid={scid:08x} log_level=warn audio=false tunnel_forward=true video_codec={self.codec} "
            f"max_size={self.max_size} video_bit_rate={self.bitrate} max_fps={self.fps} control=true "
            "cleanup=false power_off_on_close=false clipboard_autosync=false"
            + (f" video_codec_options=bitrate-mode:int={BITRATE_MODE_CBR}" if self.cbr else "")
        )
        self._proc = await asyncio.create_subprocess_exec(
            self.adb.binary, "-s", self.serial, "shell", args,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        video_r, video_w = await self._connect_video()
        control_r, control_w = await self._connect()
        self._writers = [video_w, control_w]
        await video_r.readexactly(64)  # device name
        await video_r.readexactly(4)  # codec id ("h264")
        await self._read_session(video_r)
        self._video, self._control = video_r, control_w
        self._tasks = [
            asyncio.create_task(self._pump()),
            asyncio.create_task(self._drain(control_r)),
        ]

    async def _ensure_jar(self) -> None:
        size = str(SERVER_JAR.stat().st_size)
        with contextlib.suppress(Exception):
            if (await self.adb.shell(self.serial, f"stat -c %s {DEVICE_JAR}", timeout=10)).strip() == size:
                return
        await self.adb.run("push", str(SERVER_JAR), DEVICE_JAR, serial=self.serial, timeout=60)

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", self._port)

    async def _connect_video(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """adb accepts the forward even before the server listens on the phone, then closes it.
        Retry until the server's dummy byte arrives."""
        last: Exception | None = None
        for _ in range(100):
            if self._proc and self._proc.returncode is not None:
                raise ConnectionError("scrcpy server exited on the phone")
            try:
                reader, writer = await self._connect()
                if await asyncio.wait_for(reader.readexactly(1), 5) == b"\x00":
                    return reader, writer
                writer.close()
            except (OSError, asyncio.IncompleteReadError, TimeoutError) as exc:
                last = exc
            await asyncio.sleep(0.15)
        raise ConnectionError(f"could not reach scrcpy on the phone: {last}")

    async def _read_session(self, reader: asyncio.StreamReader) -> None:
        header = await reader.readexactly(12)
        if not header[0] & 0x80:
            raise ConnectionError("unexpected scrcpy stream header")
        self.width, self.height = struct.unpack(">II", header[4:])

    async def _pump(self) -> None:
        assert self._video is not None
        try:
            while True:
                header = await self._video.readexactly(12)
                if header[0] & 0x80:  # session packet: the phone rotated / video reset
                    self.width, self.height = struct.unpack(">II", header[4:])
                    self.config = self.keyframe = b""
                    continue
                pts, size = struct.unpack(">QI", header)
                data = await self._video.readexactly(size)
                flags = (FLAG_CONFIG if pts & (1 << 62) else 0) | (FLAG_KEY if pts & (1 << 61) else 0)
                if flags & FLAG_CONFIG:
                    self.config = data
                    self._broadcast_meta()
                    continue
                if flags & FLAG_KEY:
                    self.keyframe = data
                    for fut in self._key_waiters:
                        if not fut.done():
                            fut.set_result(self.config + data)
                    self._key_waiters.clear()
                self._broadcast(flags, data)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            log.info("stream for %s ended: %s", self.serial, exc)
        finally:
            await self.stop()

    async def snapshot(self, timeout: float = 5.0) -> bytes:
        """config + a fresh keyframe (asks the encoder for one)."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._key_waiters.append(fut)
        await self.request_keyframe()
        return await asyncio.wait_for(fut, timeout)

    def _broadcast_meta(self) -> None:
        for viewer in list(self.viewers):
            _offer(viewer, ("meta", self.meta))
            _offer(viewer, ("frame", bytes([FLAG_CONFIG]) + self.config))
            viewer.needs_key = True

    def _broadcast(self, flags: int, data: bytes) -> None:
        frame = bytes([flags]) + data
        for viewer in list(self.viewers):
            if viewer.needs_key and not flags & FLAG_KEY:
                continue
            if _offer(viewer, ("frame", frame)):
                viewer.needs_key = False
            else:
                viewer.needs_key = True  # fell behind: resume at the next keyframe

    async def _drain(self, reader: asyncio.StreamReader) -> None:
        """Device messages (clipboard, acks) are not used, but must be read or the socket stalls."""
        with contextlib.suppress(Exception):
            while await reader.read(4096):
                pass

    def attach(self) -> Viewer:
        """New viewers wait for a fresh keyframe: the cached one can be minutes old, and deltas
        on top of it would draw a stale or smeared screen. join() asks the encoder for one."""
        viewer = Viewer(needs_key=True)
        if self.config:
            _offer(viewer, ("meta", self.meta))
            _offer(viewer, ("frame", bytes([FLAG_CONFIG]) + self.config))
        self.viewers.add(viewer)
        return viewer

    async def request_keyframe(self) -> None:
        await self.send(bytes([TYPE_RESET_VIDEO]))

    def detach(self, viewer: Viewer) -> None:
        self.viewers.discard(viewer)

    async def send(self, message: bytes) -> None:
        if self._control is None:
            return
        async with self._control_lock:
            self._control.write(message)
            await self._control.drain()

    async def stop(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        for viewer in list(self.viewers):
            _offer(viewer, ("end", None))
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        for writer in self._writers:
            with contextlib.suppress(Exception):
                writer.close()
        if self._proc and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
        if self._port:
            with contextlib.suppress(Exception):
                await self.adb.run("forward", "--remove", f"tcp:{self._port}", serial=self.serial)


def _offer(viewer: Viewer, item: tuple) -> bool:
    try:
        viewer.queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class StreamHub:
    """Starts a session for the first viewer of a phone and stops it after the last one leaves.

    Always the compressed profile. H.265 is used when every viewer's browser can decode it and
    the phone can encode it (about 20% smaller)."""

    def __init__(self, adb: Adb) -> None:
        self.adb = adb
        self.sessions: dict[str, ScrcpySession] = {}
        self._starting: dict[str, asyncio.Lock] = {}
        self._idle: dict[str, asyncio.Task] = {}
        self.no_h265: set[str] = set()  # phones whose encoder refused h265
        self.previews: dict[str, ScrcpySession] = {}  # small thumbnail streams
        self._preview_used: dict[str, float] = {}
        self._preview_reaper: dict[str, asyncio.Task] = {}
        self.h265_viewers: dict[str, dict] = {}  # serial -> {viewer: can_decode_h265}

    async def restart(self, serial: str) -> None:
        """Viewers see the stream end and reconnect on their own (used to switch codec)."""
        session = self.sessions.pop(serial, None)
        if session:
            await session.stop()

    async def _start(self, serial: str, codec: str) -> ScrcpySession:
        session = ScrcpySession(self.adb, serial, "low", codec)
        try:
            await asyncio.wait_for(session.start(), 30)
            return session
        except BaseException:
            await session.stop()
            if codec == "h265":  # no HEVC encoder on this phone: remember, fall back
                self.no_h265.add(serial)
                return await self._start(serial, "h264")
            raise

    async def join(self, serial: str, h265: bool = False) -> tuple[ScrcpySession, Viewer]:
        lock = self._starting.setdefault(serial, asyncio.Lock())
        async with lock:
            idle = self._idle.pop(serial, None)
            if idle:
                idle.cancel()
            session = self.sessions.get(serial)
            if session and not session.closed.is_set() and session.codec == "h265" and not h265:
                await self.restart(serial)  # this viewer cannot decode h265
                session = None
            if session is None or session.closed.is_set():
                others_ok = all(self.h265_viewers.get(serial, {}).values())
                codec = "h265" if h265 and others_ok and serial not in self.no_h265 else "h264"
                session = await self._start(serial, codec)
                self.sessions[serial] = session
                viewer = session.attach()
            else:
                viewer = session.attach()
                await session.request_keyframe()
            self.h265_viewers.setdefault(serial, {})[viewer] = h265
            return session, viewer

    async def thumbnail(self, serial: str) -> bytes:
        """JPEG of the current screen from video: ~10-20 KB instead of a 2 MB PNG.
        Uses the live session if someone is watching; otherwise a small preview stream that stays
        open while thumbnails keep being asked for (cards refresh every ~2.5 s) and closes after
        PREVIEW_IDLE_S without requests."""
        session = self.sessions.get(serial)
        if session and not session.closed.is_set():
            return await asyncio.to_thread(decode_jpeg, await session.snapshot(), session.codec)
        lock = self._starting.setdefault("preview:" + serial, asyncio.Lock())
        async with lock:
            preview = self.previews.get(serial)
            if preview is None or preview.closed.is_set():
                preview = ScrcpySession(self.adb, serial, "low", "h264", profile=THUMB_PROFILE)
                try:
                    await asyncio.wait_for(preview.start(), 20)
                except BaseException:
                    await preview.stop()
                    raise
                self.previews[serial] = preview
            self._preview_used[serial] = time.monotonic()
            if serial not in self._preview_reaper:
                self._preview_reaper[serial] = asyncio.create_task(self._reap_preview(serial))
        return await asyncio.to_thread(decode_jpeg, await preview.snapshot(8), "h264")

    async def _reap_preview(self, serial: str) -> None:
        try:
            while time.monotonic() - self._preview_used.get(serial, 0) < PREVIEW_IDLE_S:
                await asyncio.sleep(5)
            preview = self.previews.pop(serial, None)
            if preview:
                await preview.stop()
        finally:
            self._preview_reaper.pop(serial, None)

    def leave(self, serial: str, viewer: Viewer) -> None:
        self.h265_viewers.get(serial, {}).pop(viewer, None)
        session = self.sessions.get(serial)
        if session is None:
            return
        session.detach(viewer)
        if not session.viewers and serial not in self._idle:
            self._idle[serial] = asyncio.create_task(self._stop_later(serial, session))

    async def _stop_later(self, serial: str, session: ScrcpySession) -> None:
        try:
            await asyncio.sleep(IDLE_STOP_SECONDS)
            if not session.viewers:
                await session.stop()
                if self.sessions.get(serial) is session:
                    del self.sessions[serial]
        finally:
            self._idle.pop(serial, None)

    async def close(self) -> None:
        for session in [*self.sessions.values(), *self.previews.values()]:
            await session.stop()
        self.sessions.clear()
        self.previews.clear()


def decode_jpeg(annexb: bytes, codec: str = "h264", width: int = 360) -> bytes:
    """Decode one keyframe (config + IDR, Annex-B) to a small JPEG."""
    import io

    import av

    ctx = av.CodecContext.create("hevc" if codec == "h265" else "h264", "r")
    frames = ctx.decode(av.Packet(annexb)) or ctx.decode(None)
    if not frames:
        raise ValueError("keyframe did not decode")
    # Some hardware encoders (Qualcomm) tag frames with a colorspace swscale rejects (ENOTSUP);
    # screen content is BT.709 anyway, so convert as such.
    image = frames[0].reformat(format="rgb24", src_colorspace=1, dst_colorspace=1).to_image()
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=72)
    return out.getvalue()
