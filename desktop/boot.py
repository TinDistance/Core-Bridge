"""后台拉起本机中继。不碰 Tk。"""
from __future__ import annotations

import queue
import threading

from desktop.server_manager import ServerManager


class ServerBoot:
    def __init__(self) -> None:
        self._result: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._busy = False
        self.on_ready = None
        self.on_failed = None

    def start(self, port: int, on_ready, on_failed) -> None:
        if self._busy:
            return
        self._busy = True
        self.on_ready = on_ready
        self.on_failed = on_failed
        self._cancel.clear()
        threading.Thread(target=self._work, args=(int(port),), daemon=True).start()

    def cancel(self) -> None:
        self._cancel.set()

    def poll(self) -> tuple | None:
        try:
            return self._result.get_nowait()
        except queue.Empty:
            return None

    def _work(self, port: int) -> None:
        manager: ServerManager | None = None
        try:
            manager = ServerManager(port=port)
            if self._cancel.is_set():
                return
            ok = bool(manager.start())
            if self._cancel.is_set():
                threading.Thread(target=manager.stop, daemon=True).start()
                return
            error = "" if ok else (manager.last_error or "启动失败")
            self._result.put((manager, ok, error))
        except Exception as exc:
            if manager is not None:
                threading.Thread(target=manager.stop, daemon=True).start()
            try:
                self._result.put((None, False, str(exc)[:200]))
            except Exception:
                pass
        finally:
            self._busy = False
