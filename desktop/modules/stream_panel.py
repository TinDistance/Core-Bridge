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
from desktop.streaming.h264_viewer import H264Viewer, available as h264_available
from desktop.streaming.viewer import Viewer

NO_STREAM_TEXT = "没有设备在推流"
NO_STREAM_HINT = "检查 K230 是否已上电，确认推流地址指向本机 UDP 8002"
H264_SILENT_S = 5.0     # H264 连续多久没出帧就去探 JPEG 链路
SIGNAL_LOSS_GRACE_S = 5.0   # 断流宽限：之内保留最后一帧不闪"没有设备在推流"


class StreamPanel(BasePanel):
    def __init__(self, master, default_server: str = "http://127.0.0.1:8000",
                 monitor=None, **kwargs) -> None:
        self._default_server = default_server
        self._monitor = monitor  # LatencyMonitor，可选：上报拉帧耗时
        # 两条链路并存、按谁有帧用谁；旧实现是单向锁死（JPEG 不可逆），
        # 结果跑 rtp_push.py 时 JPEG 无流 -> 永久"没信号"。
        self._h264: H264Viewer | None = None
        self._jpeg: Viewer | None = None
        self._h264_last = 0.0
        self._last_frame_ts = 0.0   # 最后一次真正出帧的时刻，用于断流宽限
        self._paused = False
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
                               fg=theme.MUTE, font=theme.FONT_EYEBROW,
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

        self.after(33, self._poll)

    # ---------- 控制 ----------
    def _toggle(self) -> None:
        self._paused = not self._paused
        if self._paused:
            if self._h264 is not None:
                self._h264.stop()
                self._h264 = None
            self._stop_jpeg()
            self._streaming = False
            self._toggle_btn.config(text="继续看")
            self._set_pill("已暂停", theme.MUTE, "#232E42")
            self._redraw()
        else:
            self._h264_last = 0.0
            self._toggle_btn.config(text="暂停")

    def _ensure_h264(self) -> None:
        """H264 裸 RTP 是主链路，常驻。PyAV 缺失才退回 JPEG。"""
        if self._h264 is not None or not h264_available():
            return
        self._h264 = H264Viewer(self._server_var.get())
        self._h264.start()

    def _ensure_jpeg(self) -> None:
        """JPEG 链路只在 H264 沉默时按需拉起；H264 一恢复立刻停掉。"""
        if self._jpeg is None:
            self._jpeg = Viewer(self._server_var.get())
            self._jpeg.start()

    def _stop_jpeg(self) -> None:
        if self._jpeg is not None:
            self._jpeg.stop()
            self._jpeg = None

    def _set_pill(self, text: str, fg: str, bg: str) -> None:
        self._pill_var.set(text)
        self._pill.config(fg=fg, bg=bg)

    # ---------- 帧循环 ----------
    def _poll(self) -> None:
        now = time.monotonic()
        if self._paused:
            self.after(33, self._poll)
            return
        self._ensure_h264()

        h264_img = None
        h264_status = None
        if self._h264 is not None:
            for kind, payload in self._h264.events():
                if kind == "frame":
                    h264_img = payload
                elif kind == "status":
                    h264_status = payload

        # H264 出帧 -> 立刻收回 JPEG 链路（双向切换，不再单向锁死）
        if h264_img is not None:
            self._h264_last = now
            self._stop_jpeg()
        elif self._h264_last and now - self._h264_last < H264_SILENT_S:
            pass                        # 还在 H264 的短暂抖动窗口内
        elif h264_available():
            self._ensure_jpeg()          # H264 沉默 -> 探 JPEG

        img = h264_img
        if img is None and self._jpeg is not None:
            for kind, payload in self._jpeg.events():
                if kind == "frame":
                    img = payload
                elif kind == "status":
                    h264_status = payload

        if img is not None:
            self._last_frame_ts = now
            self._streaming = True
            t0 = time.monotonic()
            self._draw_frame(img)
            self._fetch_ms = (time.monotonic() - t0) * 1000.0
            if self._monitor is not None:
                try:
                    self._monitor.report_fetch(self._fetch_ms)
                except Exception:
                    pass
            self._set_pill("● LIVE", theme.OK, "#14352B")
            if img is not h264_img:
                self._foot_var.set(f"JPEG 回退 · {self._size_var.get()}")
        elif self._last_frame_ts and (now - self._last_frame_ts) < SIGNAL_LOSS_GRACE_S:
            # 短暂断流：保留最后一帧，不闪"没有设备在推流"。
            # 遥控/观测时一次几百毫秒的卡顿很常见，把画面清空反而更难判断
            # 是机器人停了还是链路抖了。保持 _streaming=True 也让窗口 resize
            # 时的 _redraw() 继续贴住这一帧而不是画占位符。
            self._streaming = True
            gap = now - self._last_frame_ts
            self._set_pill(f"重连中 {gap:.1f}s", theme.WARN, "#3A2E14")
        else:
            # 连续断流超过 SIGNAL_LOSS_GRACE_S（或从未收到过帧）才报无信号
            self._streaming = False
            self._photo = None
            self._size_var.set("")
            self._set_pill("无信号", theme.MUTE, "#232E42")
            self._redraw()
        self.after(33, self._poll)

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
            fill=theme.INK, font=theme.cjk_font(14),
        )
        self._canvas.create_text(
            cw // 2, ch // 2 + 14, text=NO_STREAM_HINT,
            fill=theme.MUTE, font=theme.cjk_font(9),
        )
