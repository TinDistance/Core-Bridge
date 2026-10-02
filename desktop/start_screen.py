"""启动屏：只做一件事 —— 把 server 跑起来。

品牌只有一行：TinDistance pit-wall，副标题讲清楚这是什么（小车核心控制端），
不讲故事不放插画，操作手在赛场上没时间看。
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from desktop import theme
from desktop.server_manager import ServerManager


class StartScreen(tk.Frame):
    """First screen: start the server, then hand over to the main interface."""

    def __init__(self, master, on_ready, **kwargs) -> None:
        self._on_ready = on_ready
        self._result: queue.Queue = queue.Queue()
        super().__init__(master, bg=theme.BG, **kwargs)
        self.build()

    def build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        center = tk.Frame(self, bg=theme.BG)
        center.grid(row=0, column=0, sticky="")

        card = tk.Frame(center, bg=theme.PANEL, highlightthickness=1,
                        highlightbackground=theme.LINE, padx=36, pady=32)
        card.pack()

        tk.Label(card, text="TINDISTANCE · PIT-WALL", bg=theme.PANEL,
                 fg=theme.FAINT, font=theme.FONT_EYEBROW).pack(anchor="w")
        tk.Label(card, text="Core-Bridge", bg=theme.PANEL, fg=theme.INK,
                 font=("Segoe UI Semibold", 26)).pack(anchor="w", pady=(4, 2))
        tk.Label(card, text="小车核心控制端 · 图传 / 延迟 / 日志", bg=theme.PANEL,
                 fg=theme.MUTE, font=theme.FONT_BODY).pack(anchor="w", pady=(0, 18))

        # 琥珀信号线：全启动屏唯一的高饱和元素
        tk.Frame(card, bg=theme.AMBER, height=2).pack(fill=tk.X, pady=(0, 18))

        port_row = tk.Frame(card, bg=theme.PANEL)
        port_row.pack(fill=tk.X, pady=(0, 16))
        tk.Label(port_row, text="端口", bg=theme.PANEL, fg=theme.MUTE,
                 font=theme.FONT_SMALL).pack(side=tk.LEFT)
        self._port_var = tk.StringVar(value="8000")
        ttk.Entry(port_row, textvariable=self._port_var, width=8).pack(side=tk.LEFT, padx=(8, 0))

        self._start_btn = ttk.Button(card, text="启动 Server", style="Accent.TButton",
                                      command=self._start)
        self._start_btn.pack(fill=tk.X, pady=(0, 8))

        self._status_var = tk.StringVar(value="Server 还未启动")
        tk.Label(card, textvariable=self._status_var, bg=theme.PANEL,
                 fg=theme.MUTE, font=theme.FONT_SMALL).pack()

        self._poll_result()

    def _start(self) -> None:
        try:
            port = int(self._port_var.get())
        except ValueError:
            self._status_var.set("端口无效，填 1024~65535 之间的数字")
            return

        self._start_btn.config(state=tk.DISABLED)
        self._status_var.set("正在启动 Server…")

        def work() -> None:
            manager = ServerManager(port=port)
            ok = manager.start()
            self._result.put((manager, ok))

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
            self._status_var.set("启动失败，换个端口再试一次")
            manager.stop()
            self.after(200, self._poll_result)
