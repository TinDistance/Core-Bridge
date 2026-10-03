import asyncio
import queue
import threading

import websockets


class LogClient:
    """Subscribes to the server log stream; pushes lines into a thread-safe queue."""

    MAX_QUEUE = 1000

    def __init__(self, ws_url: str, lines: queue.Queue) -> None:
        self.ws_url = ws_url
        self.lines = lines
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="log-client")
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        if self._stop_event is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except Exception:
                pass
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        finally:
            self._loop.close()
            self._loop = None
            self._thread = None

    async def _run(self) -> None:
        self._stop_event = asyncio.Event()
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.ws_url, proxy=None, open_timeout=5) as ws:
                    backoff = 1.0
                    recv_task = asyncio.create_task(self._drain(ws))
                    stop_task = asyncio.create_task(self._stop_event.wait())
                    done, _ = await asyncio.wait(
                        {recv_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        if task is recv_task:
                            task.result()
                        stop_task.cancel()
                        recv_task.cancel()
            except Exception as e:
                self._put(f"[log-client] {e}")
            if not self._stop_event.is_set():
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    def _put(self, line: str) -> None:
        try:
            if self.lines.qsize() >= self.MAX_QUEUE:
                try:
                    self.lines.get_nowait()
                except Exception:
                    pass
            self.lines.put_nowait(line)
        except Exception:
            pass

    async def _drain(self, ws) -> None:
        async for message in ws:
            self._put(message)
