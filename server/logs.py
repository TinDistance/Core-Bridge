import asyncio
import logging
from collections import deque


class BroadcastHandler(logging.Handler):
    def __init__(self, hub: "LogHub") -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        self.hub = hub

    def emit(self, record: logging.LogRecord) -> None:
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
