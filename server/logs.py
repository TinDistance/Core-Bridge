import asyncio
import logging
from collections import deque


# 桌面端高频轮询的接口：access 日志不向 WebSocket 广播，只留控制台。
_NOISY_ACCESS_PATHS = (
    "/video/status",
    "/video/latest.jpg",
    "/video/latency",
    "/video/mjpeg",
    "/ping",
    "/test",
    "/command",
)


class BroadcastHandler(logging.Handler):
    def __init__(self, hub: "LogHub") -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        self.hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        # 高频轮询接口的 access 日志只留控制台，不向桌面广播，避免刷屏
        # （桌面 LatencyMonitor 0.5s 轮询 /video/status，Viewer 30fps 轮询
        # /video/latest.jpg，全广播会把 LOGS 面板淹没）。
        if record.name == "uvicorn.access":
            try:
                msg = record.getMessage()
            except Exception:
                msg = ""
            for noisy in _NOISY_ACCESS_PATHS:
                if noisy in msg:
                    return
        self.hub._publish(self.format(record))


class LogHub:
    """Collects log records and fans them out to WebSocket subscribers."""

    def __init__(self, history_size: int = 500) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._history: deque[str] = deque(maxlen=history_size)
        self._subscribers: set[asyncio.Queue] = set()

    def attach(self, logger_names: tuple[str, ...] = ("", "uvicorn", "uvicorn.access", "uvicorn.error")) -> None:
        handler = BroadcastHandler(self)
        for name in logger_names:
            logging.getLogger(name).addHandler(handler)

    @property
    def history(self) -> deque[str]:
        return self._history

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _publish(self, line: str) -> None:
        self._history.append(line)
        if self._loop is None or self._loop.is_closed():
            return
        for q in list(self._subscribers):
            self._loop.call_soon_threadsafe(q.put_nowait, line)


hub = LogHub()
hub.attach()
