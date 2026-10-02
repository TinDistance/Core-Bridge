import tkinter as tk
from tkinter import ttk

from desktop.modules.base_panel import BasePanel
from desktop.streaming.pusher import Pusher


class StreamPanel(BasePanel):
    def __init__(self, master, default_server: str = "http://127.0.0.1:8000", **kwargs) -> None:
        self._default_server = default_server
        self._pusher: Pusher | None = None
        super().__init__(master, title="屏幕推流", **kwargs)

    def build(self) -> None:
        settings = ttk.Frame(self)
        settings.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(settings, text="Server:").pack(side=tk.LEFT)
        self._server_var = tk.StringVar(value=self._default_server)
        ttk.Entry(settings, textvariable=self._server_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        controls = ttk.Frame(self)
        controls.pack(fill=tk.X, padx=6)
        self._start_btn = ttk.Button(controls, text="开始推流", command=self._start)
        self._start_btn.pack(side=tk.LEFT)
        self._stop_btn = ttk.Button(controls, text="停止推流", command=self._stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=6)

        self._status_var = tk.StringVar(value="空闲")
        ttk.Label(self, textvariable=self._status_var).pack(fill=tk.X, padx=6, pady=6)

    def _start(self) -> None:
        if self._pusher is not None:
            return
        self._pusher = Pusher(self._server_var.get())
        self._pusher.start()
        self._start_btn.config(state=tk.DISABLED)
        self._stop_btn.config(state=tk.NORMAL)
        self._poll_status()

    def _stop(self) -> None:
        if self._pusher is None:
            return
        self._pusher.stop()
        self._pusher = None
        self._start_btn.config(state=tk.NORMAL)
        self._stop_btn.config(state=tk.DISABLED)

    def _poll_status(self) -> None:
        if self._pusher is not None and self._pusher.status():
            self._status_var.set(self._pusher.status()[-1])
            self.after(200, self._poll_status)
        elif self._pusher is not None:
            self.after(200, self._poll_status)
        else:
            self._status_var.set("空闲")
