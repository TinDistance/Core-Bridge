"""Server 日志：只回答“刚才发生了什么”，默认自动跟随，报错行标红。"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

import httpx

from desktop import theme
from desktop.logs.log_client import LogClient
from desktop.modules.base_panel import BasePanel


class LogPanel(BasePanel):
    def __init__(self, master, default_server: str = "ws://127.0.0.1:8000", **kwargs) -> None:
        self._default_server = default_server
        self._client: LogClient | None = None
        self._queue: queue.Queue = queue.Queue()
        self._follow = True
        super().__init__(master, title="LOGS · 日志", **kwargs)

    def build(self) -> None:
        head = tk.Frame(self, bg=theme.PANEL)
        head.pack(fill=tk.X, padx=theme.PAD, pady=(theme.PAD, 6))
        tk.Label(head, text="LOGS · 日志", bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.FONT_EYEBROW).pack(side=tk.LEFT)
        self._toggle_btn = ttk.Button(head, text="连接", command=self._toggle)
        self._toggle_btn.pack(side=tk.RIGHT)
        self._follow_var = tk.StringVar(value="跟随开")
        ttk.Button(head, textvariable=self._follow_var,
                   command=self._flip_follow).pack(side=tk.RIGHT, padx=(0, 6))

        self._server_var = tk.StringVar(value=self._default_server)
        addr = tk.Frame(self, bg=theme.PANEL)
        addr.pack(fill=tk.X, padx=theme.PAD, pady=(0, 6))
        tk.Label(addr, text="Server", bg=theme.PANEL, fg=theme.MUTE,
                 font=theme.FONT_SMALL).pack(side=tk.LEFT)
        ttk.Entry(addr, textvariable=self._server_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        frame = tk.Frame(self, bg=theme.PANEL)
        frame.pack(fill=tk.BOTH, expand=True, padx=theme.PAD, pady=(0, theme.PAD))
        self._text = tk.Text(frame, wrap=tk.WORD, state=tk.DISABLED,
                             bg="#0B0F16", fg="#C7D2E2", insertbackground=theme.INK,
                             relief=tk.FLAT, highlightthickness=1,
                             highlightbackground=theme.LINE,
                             font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._text.yview)
        self._text.configure(yscrollcommand=scrollbar.set)
        self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._text.tag_config("err", foreground=theme.BAD)
        self._text.tag_config("warn", foreground=theme.WARN)
        self._text.tag_config("ok", foreground=theme.OK)

        self.after(0, self._clear_remote_history)
        self.after(200, self._poll)

    # ---------- 连接 ----------
    def _clear_remote_history(self) -> None:
        url = self._default_server.rstrip("/")
        threading.Thread(target=self._clear_worker, args=(url,), daemon=True).start()

    def _clear_worker(self, url: str) -> None:
        try:
            httpx.delete(f"{url}/logs", timeout=2)
        except Exception:
            pass

    def _toggle(self) -> None:
        if self._client is None:
            self._queue = queue.Queue()
            self._reset_text()
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

    def _flip_follow(self) -> None:
        self._follow = not self._follow
        self._follow_var.set("跟随开" if self._follow else "跟随关")

    def _reset_text(self) -> None:
        self._text.config(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.config(state=tk.DISABLED)

    # ---------- 输出 ----------
    def _poll(self) -> None:
        self._text.config(state=tk.NORMAL)
        while True:
            try:
                line = self._queue.get_nowait()
            except queue.Empty:
                break
            tag = None
            low = line.lower()
            if "error" in low or "exception" in low or "failed" in low or "err:" in low:
                tag = "err"
            elif "warn" in low:
                tag = "warn"
            elif "ok" in low or "done" in low or "connected" in low:
                tag = "ok"
            if tag:
                self._text.insert(tk.END, line + "\n", tag)
            else:
                self._text.insert(tk.END, line + "\n")
            if float(self._text.index(tk.END).split(".")[0]) > 2000:
                self._text.delete("1.0", "500.0")
        if self._follow:
            self._text.see(tk.END)
        self._text.config(state=tk.DISABLED)
        self.after(200, self._poll)
