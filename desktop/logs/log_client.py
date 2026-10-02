import asyncio
import queue
import threading

import websockets


class LogClient:
    """Subscribes to the server log stream; pushes lines into a thread-safe queue."""

    def __init__(self, ws_url: str, lines: queue.Queue) -> None:
        self.ws_url = ws_url
        self.lines = lines
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="log-client")
        self._thread.start()

    def stop(self) -> None:
        if self._stop_event is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)

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
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.ws_url, proxy=None) as ws:
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
                self.lines.put(f"[log-client] {e}")
            if not self._stop_event.is_set():
                await asyncio.sleep(3)

    async def _drain(self, ws) -> None:
        async for message in ws:
            self.lines.put(message)
