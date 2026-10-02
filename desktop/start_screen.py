import queue
import tkinter as tk
from tkinter import ttk

from desktop.server_manager import ServerManager


class StartScreen(ttk.Frame):
    """First screen: start the server, then hand over to the main interface."""

    def __init__(self, master, on_ready, **kwargs) -> None:
        self._on_ready = on_ready
        self._result: queue.Queue = queue.Queue()
        self._manager: ServerManager | None = None
        super().__init__(master, **kwargs)
        self.build()

    def build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        card = ttk.Frame(self)
        card.grid(row=0, column=0, sticky="")

        ttk.Label(card, text="Core-Bridge", font=("Segoe UI", 22, "bold")).grid(row=0, column=0, pady=(0, 20))
        ttk.Label(card, text="Server 还未启动").grid(row=1, column=0, pady=(0, 10))

        port_row = ttk.Frame(card)
        port_row.grid(row=2, column=0, pady=(0, 16))
        ttk.Label(port_row, text="端口:").pack(side=tk.LEFT)
        self._port_var = tk.StringVar(value="8000")
        ttk.Entry(port_row, textvariable=self._port_var, width=8).pack(side=tk.LEFT, padx=6)

        self._start_btn = ttk.Button(card, text="启动 Server", command=self._start)
        self._start_btn.grid(row=3, column=0, pady=(0, 12))

        self._status_var = tk.StringVar(value="")
        ttk.Label(card, textvariable=self._status_var).grid(row=4, column=0, pady=(0, 12))

        self._poll_result()

    def _start(self) -> None:
        try:
            port = int(self._port_var.get())
        except ValueError:
            self._status_var.set("端口无效")
            return

        self._start_btn.config(state=tk.DISABLED)
        self._status_var.set("正在启动 Server…")

        def work() -> None:
            manager = ServerManager(port=port)
            ok = manager.start()
            self._result.put((manager, ok))

        import threading

        threading.Thread(target=work, daemon=True).start()

    def _poll_result(self) -> None:
        try:
            manager, ok = self._result.get_nowait()
        except queue.Empty:
            self.after(200, self._poll_result)
            return
        if ok:
            self._status_var.set("启动成功")
            self._on_ready(manager)
        else:
            self._start_btn.config(state=tk.NORMAL)
            self._status_var.set("启动失败，请检查端口后重试")
            manager.stop()
            self._manager = None
            self.after(200, self._poll_result)
