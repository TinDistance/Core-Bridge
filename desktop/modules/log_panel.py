import queue
import tkinter as tk
from tkinter import ttk

from desktop.logs.log_client import LogClient
from desktop.modules.base_panel import BasePanel


class LogPanel(BasePanel):
    def __init__(self, master, default_server: str = "ws://127.0.0.1:8000", **kwargs) -> None:
        self._default_server = default_server
        self._client: LogClient | None = None
        self._queue: queue.Queue = queue.Queue()
        super().__init__(master, title="Server 日志", **kwargs)

    def build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(top, text="Server:").pack(side=tk.LEFT)
        self._server_var = tk.StringVar(value=self._default_server)
        ttk.Entry(top, textvariable=self._server_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        self._toggle_btn = ttk.Button(top, text="连接", command=self._toggle)
        self._toggle_btn.pack(side=tk.LEFT)

        frame = ttk.Frame(self)
        frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self._text = tk.Text(frame, wrap=tk.WORD, state=tk.DISABLED, font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._text.yview)
        self._text.configure(yscrollcommand=scrollbar.set)
        self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.after(200, self._poll)

    def _toggle(self) -> None:
        if self._client is None:
            self._queue = queue.Queue()
            url = self._server_var.get().rstrip("/")
            if url.startswith("http://"):
                url = "ws://" + url[len("http://"):]
            elif url.startswith("https://"):
                url = "wss://" + url[len("https://"):]
            self._client = LogClient(url + "/ws/logs", self._queue)
            self._client.start()
            self._toggle_btn.config(text="断开")
        else:
            self._client.stop()
            self._client = None
            self._toggle_btn.config(text="连接")

    def _poll(self) -> None:
        self._text.config(state=tk.NORMAL)
        while True:
            try:
                line = self._queue.get_nowait()
            except queue.Empty:
                break
            self._text.insert(tk.END, line + "\n")
            if float(self._text.index(tk.END).split(".")[0]) > 2000:
                self._text.delete("1.0", "500.0")
        self._text.see(tk.END)
        self._text.config(state=tk.DISABLED)
        self.after(200, self._poll)
