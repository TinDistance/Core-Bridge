"""手柄后台推送：XInput ~100Hz 轮询 -> 16 字节协议 -> POST /command。

策略（ pit-wall 原则：不断线、不刷屏、不卡 UI ）：
- 独立 daemon 线程轮询，不碰 Tk；UI 经 snapshot() 拉最新帧。
- 有变化即推；无变化 500ms 补一次心跳，保证 K230 轮询 GET 时 age_ms 可信。
- 无手柄时不 POST 脏数据，只更新本地 snapshot（中位报文），server 保留上一帧。
- server 未就绪时吞错重试，不抛、不刷日志。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx

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
    """应用启动即 start()：先尝试连接手柄，全程后台重连 + 实时 POST。"""

    def __init__(self, get_base_url, poll_hz: float = 100.0) -> None:
        self._get_base_url = get_base_url
        self._interval = 1.0 / max(1.0, min(poll_hz, 120.0))
        self._reader = xi.XInputReader()  # 构造时已尝试 find_controller
        self._lock = threading.Lock()
        self._snap = GamepadSnapshot(
            connected=self._reader.slot is not None,
            slot=self._reader.slot,
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frames = 0
        self._t0 = time.monotonic()

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="gamepad-pusher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> GamepadSnapshot:
        with self._lock:
            s = self._snap
            return GamepadSnapshot(
                connected=s.connected, slot=s.slot, packet=s.packet,
                proto=s.proto, buttons=s.buttons, lt8=s.lt8, rt8=s.rt8,
                lx=s.lx, ly=s.ly, rx=s.rx, ry=s.ry,
                fps=s.fps, posted=s.posted, last_error=s.last_error,
            )

    # ---------- 主循环 ----------
    def _loop(self) -> None:
        last_post = 0.0
        last_proto: bytes | None = None
        try:
            with httpx.Client(timeout=1.5, trust_env=False) as client:
                while not self._stop.is_set():
                    t0 = time.monotonic()
                    st = self._reader.poll()
                    now = time.time()

                    # fps 统计（1s 窗口）
                    self._frames += 1
                    dt = t0 - self._t0
                    fps = self._frames / dt if dt >= 1.0 else self._snap.fps
                    if dt >= 1.0:
                        self._frames = 0
                        self._t0 = t0

                    posted = self._snap.posted
                    err = ""
                    if st.connected:
                        changed = last_proto is None or st.proto != last_proto
                        heartbeat = (now - last_post) >= 0.5
                        if changed or heartbeat:
                            ok, e = self._post(client, st, now)
                            if ok:
                                last_proto = st.proto
                                last_post = now
                                posted = True
                            else:
                                err = e
                    with self._lock:
                        self._snap = GamepadSnapshot(
                            connected=st.connected, slot=st.slot,
                            packet=st.packet, proto=st.proto,
                            buttons=st.buttons, lt8=st.lt8, rt8=st.rt8,
                            lx=st.lx, ly=st.ly, rx=st.rx, ry=st.ry,
                            fps=round(fps, 1),
                            posted=posted,
                            last_error=err or self._snap.last_error if not st.connected else err,
                        )
                    spent = time.monotonic() - t0
                    rest = self._interval - spent
                    if rest > 0:
                        self._stop.wait(rest)
        finally:
            self._thread = None

    def _post(self, client: httpx.Client, st: xi.PadState, now: float) -> tuple[bool, str]:
        try:
            base = (self._get_base_url() or "").rstrip("/")
        except Exception:
            base = ""
        if not base:
            return False, ""
        try:
            parsed = xi.parse_protocol(st.proto)
            resp = client.post(f"{base}/command", json={
                "raw_hex": parsed["raw_hex"],
                "slot": st.slot,
                "packet": st.packet,
                "client_ts": round(now, 3),
            })
            if resp.status_code == 200:
                return True, ""
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)[:120]
