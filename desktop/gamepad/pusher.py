"""手柄后台推送：XInput ~100Hz 轮询 -> 16 字节协议 -> POST /command。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx

from desktop.gamepad import commands as cmd_builder
from desktop.gamepad import xinput as xi


@dataclass
class GamepadSnapshot:
    connected: bool = False
    slot: int | None = None
    packet: int = 0
    proto: bytes = b"\x00\x80\x00\x80\x00\x80\x00\x80\x00\x00\x00\x00\x00\x00\x00\x00"
    buttons: int = 0
    lt8: int = 0
    rt8: int = 0
    lx: int = 0
    ly: int = 0
    rx: int = 0
    ry: int = 0
    fps: float = 0.0
    posted: bool = False
    last_error: str = ""


class GamepadPusher:
    """应用启动即 start()：先尝试连接手柄，全程后台重连 + 实时 POST。

    断连/回中时主动补发一次中位帧，避免 server 侧残留旧偏转（failsafe）。
    """

    POST_TIMEOUT = 0.3
    HEARTBEAT_S = 0.5

    def __init__(self, get_base_url, poll_hz: float = 100.0) -> None:
        self._get_base_url = get_base_url
        self._interval = 1.0 / max(1.0, min(poll_hz, 120.0))
        self._reader = xi.XInputReader()
        self._lock = threading.Lock()
        self._snap = GamepadSnapshot(
            connected=self._reader.slot is not None,
            slot=self._reader.slot,
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frames = 0
        self._t0 = time.monotonic()
        self._seq = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="gamepad-pusher")
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)

    def snapshot(self) -> GamepadSnapshot:
        with self._lock:
            s = self._snap
            return GamepadSnapshot(
                connected=s.connected, slot=s.slot, packet=s.packet,
                proto=s.proto, buttons=s.buttons, lt8=s.lt8, rt8=s.rt8,
                lx=s.lx, ly=s.ly, rx=s.rx, ry=s.ry,
                fps=s.fps, posted=s.posted, last_error=s.last_error,
            )

    def _loop(self) -> None:
        last_post = 0.0
        last_proto: bytes | None = None
        neutral_sent = False
        try:
            with httpx.Client(timeout=self.POST_TIMEOUT, trust_env=False) as client:
                while not self._stop.is_set():
                    t0 = time.monotonic()
                    st = self._reader.poll()
                    now = time.time()

                    self._frames += 1
                    with self._lock:
                        prev_fps = self._snap.fps
                    dt = t0 - self._t0
                    fps = self._frames / dt if dt >= 1.0 else prev_fps
                    if dt >= 1.0:
                        self._frames = 0
                        self._t0 = t0

                    posted = False
                    err = ""
                    if st.connected:
                        neutral_sent = False
                        changed = last_proto is None or st.proto != last_proto
                        heartbeat = (now - last_post) >= self.HEARTBEAT_S
                        if changed or heartbeat:
                            ok, e = self._post(client, st, now)
                            if ok:
                                last_proto = st.proto
                                last_post = now
                                posted = True
                            else:
                                err = e
                                # 沿用上次 posted 状态，避免闪烁；错误如实上报
                                with self._lock:
                                    posted = self._snap.posted
                        else:
                            with self._lock:
                                posted = self._snap.posted
                    else:
                        # 断连 failsafe：补发一次中位帧，让 server 回中
                        if not neutral_sent:
                            ok, e = self._post_neutral(client, now)
                            neutral_sent = ok
                            last_proto = None
                            last_post = now if ok else last_post
                            err = "" if ok else (e or "等待手柄")
                        else:
                            err = "等待手柄"
                        posted = False
                    with self._lock:
                        self._snap = GamepadSnapshot(
                            connected=st.connected, slot=st.slot,
                            packet=st.packet, proto=st.proto,
                            buttons=st.buttons, lt8=st.lt8, rt8=st.rt8,
                            lx=st.lx, ly=st.ly, rx=st.rx, ry=st.ry,
                            fps=round(fps, 1),
                            posted=posted,
                            last_error=err,
                        )
                    spent = time.monotonic() - t0
                    rest = self._interval - spent
                    if rest > 0:
                        self._stop.wait(rest)
        finally:
            self._thread = None

    def _post_neutral(self, client: httpx.Client, now: float) -> tuple[bool, str]:
        try:
            base = (self._get_base_url() or "").rstrip("/")
        except Exception:
            base = ""
        if not base:
            return False, ""
        try:
            proto = xi.neutral_protocol()
            parsed = xi.parse_protocol(proto)
            self._seq += 1
            resp = client.post(f"{base}/command", json={
                "raw_hex": parsed["raw_hex"],
                "cmds": cmd_builder.neutral_commands(),
                "slot": None,
                "packet": 0,
                "client_ts": round(now, 3),
                "seq": self._seq,
            })
            if resp.status_code == 200:
                return True, ""
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)[:120]

    def _post(self, client: httpx.Client, st: xi.PadState, now: float) -> tuple[bool, str]:
        try:
            base = (self._get_base_url() or "").rstrip("/")
        except Exception:
            base = ""
        if not base:
            return False, ""
        try:
            parsed = xi.parse_protocol(st.proto)
            cmds = cmd_builder.build_commands(
                parsed["lx"], parsed["ly"], parsed["rx"], parsed["ry"])
            self._seq += 1
            resp = client.post(f"{base}/command", json={
                "raw_hex": parsed["raw_hex"],
                "cmds": cmds,
                "slot": st.slot,
                "packet": st.packet,
                "client_ts": round(now, 3),
                "seq": self._seq,
            })
            if resp.status_code == 200:
                return True, ""
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)[:120]
