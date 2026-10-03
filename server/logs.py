import asyncio
import logging
import threading
from collections import deque


_NOISY_ACCESS_PATHS = (
    "/video/status",
    "/video/latest.jpg",
    "/video/timing",
    "/video/mjpeg",
    "/ping",
    "/test",
    "/command",
)

SUBSCRIBER_QUEUE_MAX = 500


class BroadcastHandler(logging.Handler):
    def __init__(self, hub: "LogHub") -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        self.hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "uvicorn.access":
            try:
                msg = record.getMessage()
            except Exception:
                msg = ""
            for noisy in _NOISY_ACCESS_PATHS:
                if noisy in msg:
                    return
        try:
            self.hub.publish(self.format(record))
        except Exception:
            pass


class LogHub:
    """Collects log records and fans them out to WebSocket subscribers."""

    def __init__(self, history_size: int = 500) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._history: deque[str] = deque(maxlen=history_size)
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._attached = False

    def attach(self, logger_names: tuple[str, ...] = ("", "uvicorn", "uvicorn.access", "uvicorn.error")) -> None:
        if self._attached:
            return
        handler = BroadcastHandler(self)
        for name in logger_names:
            logging.getLogger(name).addHandler(handler)
        self._attached = True

    @property
    def history(self) -> deque[str]:
        return self._history

    def set_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, line: str) -> None:
        """公有发布入口（线程安全）。"""
        self._publish(line)

    def _publish(self, line: str) -> None:
        with self._lock:
            self._history.append(line)
            subs = list(self._subscribers)
            loop = self._loop
        if loop is None:
            return
        try:
            if loop.is_closed():
                return
        except Exception:
            return
        for q in subs:
            try:
                loop.call_soon_threadsafe(self._enqueue_drop_oldest, q, line)
            except RuntimeError:
                pass

    @staticmethod
    def _enqueue_drop_oldest(q: asyncio.Queue, line: str) -> None:
        try:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(line)
        except Exception:
            pass


hub = LogHub()
hub.attach()
