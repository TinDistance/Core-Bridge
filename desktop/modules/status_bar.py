"""底状态条。启动失败时才出现端口和重试。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from desktop import theme


class StatusBar(tk.Frame):
    def __init__(self, master, on_retry) -> None:
        super().__init__(master, bg=theme.PANEL, highlightthickness=1,
                         highlightbackground=theme.LINE)
        self._on_retry = on_retry
        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status, bg=theme.PANEL, fg=theme.MUTE,
                 font=theme.FONT_MONO_SM).pack(side=tk.LEFT, padx=12, pady=5)
        self._retry_box = tk.Frame(self, bg=theme.PANEL)
        self._port = tk.StringVar(value="8000")
        ttk.Entry(self._retry_box, textvariable=self._port, width=8).pack(
            side=tk.LEFT, padx=(0, 6))
        ttk.Button(self._retry_box, text="重试", command=self._retry).pack(
            side=tk.LEFT)
        self._retry_box.pack(side=tk.RIGHT, padx=12, pady=4)
        self.show_retry(False)

    def set_status(self, text: str) -> None:
        self._status.set(text)

    def show_retry(self, visible: bool) -> None:
        if visible:
            self._retry_box.pack(side=tk.RIGHT, padx=12, pady=4)
        else:
            self._retry_box.pack_forget()

    def _retry(self) -> None:
        raw = self._port.get().strip()
        try:
            port = int(raw)
        except ValueError:
            self._status.set("端口无效，填 1024~65535 之间的数字")
            return
        if not 1024 <= port <= 65535:
            self._status.set("端口无效，填 1024~65535 之间的数字")
            return
        if self._on_retry is not None:
            self._on_retry(port)
