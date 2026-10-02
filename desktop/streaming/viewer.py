import io
import threading
import time
from collections import deque
from typing import Union

import httpx
from PIL import Image

Event = tuple[str, Union[str, "Image.Image"]]


class Viewer:
    """经 HTTP 拉取 server 重组好的 JPEG 帧并产出 PIL 帧。

    链路：K230 UDP 分片 -> server:8001 重组 -> GET /video/latest.jpg。
    只收完整帧（server 已做 SOI/EOI 校验），本端解码失败则丢帧不崩。

    Events: ("frame", PIL.Image) | ("status", "streaming"|"no_stream"|text)
    接口与旧 WebRTC Viewer 一致，StreamPanel 无需改动。
    """

    def __init__(self, server_url: str, fps: int = 12) -> None:
        self.server_url = server_url.rstrip("/")
        self.fps = max(1, min(fps, 20))
        # 帧队列只留最新几帧，避免网络抖动时延迟堆积
        self._events: deque[Event] = deque(maxlen=8)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_id: int | None = None
        self._streaming = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="jpeg-viewer")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def events(self) -> list[Event]:
        with self._lock:
            out = list(self._events)
            self._events.clear()
        return out

    def _emit(self, event: Event) -> None:
        with self._lock:
            self._events.append(event)

    def _set_streaming(self, on: bool) -> None:
        if on == self._streaming:
            return
        self._streaming = on
        self._emit(("status", "streaming" if on else "no_stream"))

    def _thread_main(self) -> None:
        interval = 1.0 / self.fps
        fail_streak = 0
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                while not self._stop.is_set():
                    t0 = time.monotonic()
                    try:
                        url = f"{self.server_url}/video/latest.jpg"
                        params = {"since": self._last_id} if self._last_id is not None else None
                        resp = client.get(url, params=params)
                        if resp.status_code == 304:
                            pass  # 无新帧，等下一拍
                        elif resp.status_code == 404:
                            self._last_id = None
                            self._set_streaming(False)
                            fail_streak = 0
                        elif resp.status_code == 200:
                            fail_streak = 0
                            fid = resp.headers.get("X-Frame-Id")
                            try:
                                img = Image.open(io.BytesIO(resp.content))
                                img.load()  # 在网络线程内解码完，避免懒加载跨线程问题
                            except Exception:
                                pass  # 坏帧直接丢，不更新 last_id 等下一帧
                            else:
                                if fid is not None:
                                    try:
                                        self._last_id = int(fid)
                                    except ValueError:
                                        pass
                                self._set_streaming(True)
                                self._emit(("frame", img))
                        else:
                            fail_streak += 1
                            if fail_streak == 1:
                                self._emit(("status", f"视频流异常: HTTP {resp.status_code}"))
                    except Exception as e:
                        fail_streak += 1
                        # 降频打日志：只在第一次失败/恢复时提示，避免刷屏
                        if fail_streak == 1:
                            self._emit(("status", f"连接失败: {e}"))
                        self._set_streaming(False)
                        time.sleep(1.5)
                        continue
                    dt = time.monotonic() - t0
                    rest = interval - dt
                    if rest > 0:
                        self._stop.wait(rest)
        finally:
            self._thread = None
