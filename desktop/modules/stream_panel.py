"""推流画面：全场最大的一块，操作手的眼睛 80% 时间在这里。

空状态写清楚怎么办（检查 K230 是否上电推流），不断重复系统术语；
状态药丸只讲人话：LIVE / 无信号 / 已暂停。
"""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from desktop import theme
from desktop.modules.base_panel import BasePanel
from desktop.streaming.viewer import Viewer

NO_STREAM_TEXT = "没有设备在推流"
NO_STREAM_HINT = "检查 K230 是否已上电，确认推流地址指向本机 UDP 8001"


class StreamPanel(BasePanel):
    def __init__(self, master, default_server: str = "http://127.0.0.1:8000",
                 monitor=None, **kwargs) -> None:
        self._default_server = default_server
        self._monitor = monitor  # LatencyMonitor，可选：上报拉帧耗时
        self._viewer: Viewer | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._streaming = False
        self._frames = 0
        self._fetch_ms = 0.0
        super().__init__(master, title="VIDEO · 图传", **kwargs)

    def build(self) -> None:
        head = tk.Frame(self, bg=theme.PANEL)
        head.pack(fill=tk.X, padx=theme.PAD, pady=(theme.PAD, 6))
        tk.Label(head, text="VIDEO · 图传", bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.FONT_EYEBROW).pack(side=tk.LEFT)
        self._pill_var = tk.StringVar(value="无信号")
        self._pill = tk.Label(head, textvariable=self._pill_var, bg="#232E42",
                              fg=theme.MUTE, font=("Segoe UI Semibold", 8),
                              padx=8, pady=2)
        self._pill.pack(side=tk.RIGHT)
        self._meta_var = tk.StringVar(value="")
        tk.Label(head, textvariable=self._meta_var, bg=theme.PANEL,
                 fg=theme.FAINT, font=theme.FONT_MONO_SM).pack(side=tk.RIGHT, padx=(0, 8))

        # 控制条：地址 + 观看开关（主操作，琥珀）
        bar = tk.Frame(self, bg=theme.PANEL)
        bar.pack(fill=tk.X, padx=theme.PAD, pady=(0, 6))
        tk.Label(bar, text="Server", bg=theme.PANEL, fg=theme.MUTE,
                 font=theme.FONT_SMALL).pack(side=tk.LEFT)
        self._server_var = tk.StringVar(value=self._default_server)
        ttk.Entry(bar, textvariable=self._server_var, width=26).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        self._toggle_btn = ttk.Button(bar, text="暂停", style="Accent.TButton",
                                      command=self._toggle)
        self._toggle_btn.pack(side=tk.LEFT)

        self._canvas = tk.Canvas(self, bg="#0B0F16", highlightthickness=1,
                                 highlightbackground=theme.LINE)
        self._canvas.pack(fill=tk.BOTH, expand=True, padx=theme.PAD, pady=0)
        self._canvas.bind("<Configure>", lambda _: self._redraw())

        foot = tk.Frame(self, bg=theme.PANEL)
        foot.pack(fill=tk.X, padx=theme.PAD, pady=(6, theme.PAD))
        self._foot_var = tk.StringVar(value="等待首帧…")
        tk.Label(foot, textvariable=self._foot_var, bg=theme.PANEL,
                 fg=theme.MUTE, font=theme.FONT_MONO_SM).pack(side=tk.LEFT)
        self._size_var = tk.StringVar(value="")
        tk.Label(foot, textvariable=self._size_var, bg=theme.PANEL,
                 fg=theme.FAINT, font=theme.FONT_MONO_SM).pack(side=tk.RIGHT)

        self.after(500, self._ensure_viewer)
        self.after(100, self._poll)

    # ---------- 控制 ----------
    def _toggle(self) -> None:
        if self._viewer is None:
            self._ensure_viewer()
            self._toggle_btn.config(text="暂停")
        else:
            self._viewer.stop()
            self._viewer = None
            self._streaming = False
            self._toggle_btn.config(text="继续看")
            self._set_pill("已暂停", theme.MUTE, "#232E42")
            self._redraw()

    def _ensure_viewer(self) -> None:
        if self._viewer is None:
            self._viewer = Viewer(self._server_var.get())
            self._viewer.start()

    def _set_pill(self, text: str, fg: str, bg: str) -> None:
        self._pill_var.set(text)
        self._pill.config(fg=fg, bg=bg)

    # ---------- 帧循环 ----------
    def _poll(self) -> None:
        if self._viewer is not None:
            for kind, payload in self._viewer.events():
                if kind == "frame":
                    t0 = time.monotonic()
                    self._streaming = True
                    self._draw_frame(payload)
                    self._fetch_ms = (time.monotonic() - t0) * 1000.0
                    if self._monitor is not None:
                        try:
                            self._monitor.report_fetch(self._fetch_ms)
                        except Exception:
                            pass
                    self._set_pill("● LIVE", theme.OK, "#14352B")
                elif kind == "status":
                    if payload == "streaming":
                        self._streaming = True
                        self._set_pill("● LIVE", theme.OK, "#14352B")
                    elif payload == "no_stream":
                        self._streaming = False
                        self._set_pill("无信号", theme.MUTE, "#232E42")
                        self._redraw()
                    else:
                        self._set_pill("连接中", theme.WARN, "#3A2E14")
                        self._foot_var.set(str(payload))
        self.after(100, self._poll)

    def _current_image_size(self) -> tuple[int, int]:
        if self._photo is None:
            return (0, 0)
        return (self._photo.width(), self._photo.height())

    def _redraw(self) -> None:
        if self._streaming and self._photo is not None:
            # 重排时保持最后一帧居中即可，不重采样省 CPU
            cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
            self._canvas.delete("all")
            if cw > 2 and ch > 2:
                self._canvas.create_image(cw // 2, ch // 2, image=self._photo)
        else:
            self._draw_placeholder()

    def _draw_frame(self, img: Image.Image) -> None:
        cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        if cw < 2 or ch < 2:
            return
        fw, fh = img.size
        scale = min(cw / fw, ch / fh)
        nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
        if (nw, nh) != (fw, fh):
            img = img.resize((nw, nh))
        self._photo = ImageTk.PhotoImage(img)
        self._canvas.delete("all")
        self._canvas.create_image(cw // 2, ch // 2, image=self._photo)
        self._frames += 1
        self._foot_var.set(f"#{self._frames} · {fw}x{fh} → {nw}x{nh}")
        self._size_var.set(f"{fw}x{fh}")

    def _draw_placeholder(self) -> None:
        if self._streaming:
            return
        cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        self._canvas.delete("all")
        if cw < 2 or ch < 2:
            return
        self._canvas.create_text(
            cw // 2, ch // 2 - 12, text=NO_STREAM_TEXT,
            fill=theme.MUTE, font=("Segoe UI Semibold", 14),
        )
        self._canvas.create_text(
            cw // 2, ch // 2 + 14, text=NO_STREAM_HINT,
            fill=theme.FAINT, font=("Segoe UI", 9),
        )
