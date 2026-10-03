"""推流画面：全场最大的一块，操作手的眼睛 80% 时间在这里。"""
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
H264_SILENT_S = 5.0
SIGNAL_LOSS_GRACE_S = 5.0


class StreamPanel(BasePanel):
    def __init__(self, master, default_server: str = "http://127.0.0.1:8000",
                 monitor=None, **kwargs) -> None:
        self._default_server = default_server
        self._monitor = monitor
        self._h264: H264Viewer | None = None
        self._jpeg: Viewer | None = None
        self._h264_last = 0.0
        self._last_frame_ts = 0.0
        self._last_status = ""
        self._paused = False
        self._closed = False
        self._photo: ImageTk.PhotoImage | None = None
        self._pil_cache: Image.Image | None = None
        self._streaming = False
        self._frames = 0
        self._draw_ms = 0.0
        self._poll_after: str | None = None
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
        try:
            self._server_var.trace_add("write", lambda *_: self._on_server_changed())
        except Exception:
            pass

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

        # 接线延迟探针：provider 模式（单入口），render_age 不再恒 -1
        if self._monitor is not None:
            try:
                self._monitor.set_stream_stats_provider(
                    lambda: self._h264.stats() if self._h264 is not None else {})
            except Exception:
                pass
        self._poll_after = self.after(33, self._poll)

    def destroy(self) -> None:  # type: ignore[override]
        self._closed = True
        try:
            if self._poll_after is not None:
                self.after_cancel(self._poll_after)
        except Exception:
            pass
        try:
            if self._h264 is not None:
                self._h264.stop()
        except Exception:
            pass
        try:
            self._stop_jpeg()
        except Exception:
            pass
        super().destroy()

    def _on_server_changed(self) -> None:
        """地址编辑后重建 viewer（防抖：由下次 _poll 实际执行）。"""
        # 标记强制重建：停掉旧对象，下轮 _ensure 会用新地址创建
        try:
            if self._h264 is not None:
                self._h264.stop()
                self._h264 = None
            self._stop_jpeg()
            self._h264_last = 0.0
        except Exception:
            pass

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
            self._h264_last = time.monotonic()
            self._toggle_btn.config(text="暂停")

    def _ensure_h264(self) -> None:
        """H264 裸 RTP 是主链路，常驻。PyAV 缺失才退回 JPEG。"""
        if self._h264 is not None or not h264_available():
            return
        self._h264 = H264Viewer(self._server_var.get())
        self._h264.start()
        # 预热窗口：刚创建尚未完成 IDR 握手，避免下一轮误拉 JPEG 双耗
        self._h264_last = time.monotonic()

    def _ensure_jpeg(self) -> None:
        """JPEG 链路只在 H264 沉默时按需拉起；H264 一恢复立刻停掉。"""
        if self._jpeg is None:
            self._jpeg = Viewer(self._server_var.get())
            self._jpeg.start()

    def _stop_jpeg(self) -> None:
        if self._jpeg is not None:
            try:
                self._jpeg.stop()
            except Exception:
                pass
            self._jpeg = None

    def _set_pill(self, text: str, fg: str, bg: str) -> None:
        self._pill_var.set(text)
        self._pill.config(fg=fg, bg=bg)

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            now = time.monotonic()
            if self._paused:
                return
            self._ensure_h264()
            # 无 PyAV：直接 JPEG（原 elif 逻辑导致黑屏）
            if not h264_available():
                self._ensure_jpeg()

            h264_img = None
            h264_status = None
            if self._h264 is not None:
                for kind, payload in self._h264.events():
                    if kind == "frame":
                        h264_img = payload
                    elif kind == "status":
                        h264_status = payload
                        if payload == "fallback":
                            # 主链路明确 fallback：立即拉起 JPEG
                            self._ensure_jpeg()

            if h264_img is not None:
                self._h264_last = now
                self._stop_jpeg()
            elif self._h264_last and now - self._h264_last < H264_SILENT_S:
                pass
            else:
                self._ensure_jpeg()

            img = h264_img
            jpeg_status = None
            if img is None and self._jpeg is not None:
                for kind, payload in self._jpeg.events():
                    if kind == "frame":
                        img = payload
                    elif kind == "status":
                        jpeg_status = payload

            status_text = h264_status or jpeg_status
            if status_text:
                self._last_status = str(status_text)

            if img is not None:
                self._last_frame_ts = now
                self._streaming = True
                t0 = time.monotonic()
                self._draw_frame(img)
                self._draw_ms = (time.monotonic() - t0) * 1000.0
                if self._monitor is not None:
                    try:
                        self._monitor.report_draw(self._draw_ms)
                    except Exception:
                        pass
                self._set_pill("● LIVE", theme.OK, "#14352B")
                if img is not h264_img:
                    self._foot_var.set(f"JPEG 回退 · {self._size_var.get()} · {self._last_status}")
                elif self._last_status and self._last_status not in ("streaming",):
                    self._foot_var.set(f"{self._last_status} · {self._size_var.get()}")
            elif self._last_frame_ts and (now - self._last_frame_ts) < SIGNAL_LOSS_GRACE_S:
                self._streaming = True
                gap = now - self._last_frame_ts
                self._set_pill(f"重连中 {gap:.1f}s", theme.WARN, "#3A2E14")
                if self._last_status:
                    self._foot_var.set(f"{self._last_status} · 重连中 {gap:.1f}s")
            else:
                self._streaming = False
                self._photo = None
                self._size_var.set("")
                self._set_pill("无信号", theme.MUTE, "#232E42")
                self._redraw()
        finally:
            if not self._closed:
                try:
                    self._poll_after = self.after(33, self._poll)
                except Exception:
                    pass

    def _redraw(self) -> None:
        # 用缓存的 PIL 原图按新尺寸重缩放，避免拉伸滞后
        if self._streaming and self._pil_cache is not None:
            try:
                self._draw_frame(self._pil_cache, cache=False)
                return
            except Exception:
                pass
        if self._streaming and self._photo is not None:
            try:
                cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
            except tk.TclError:
                return
            self._canvas.delete("all")
            if cw > 2 and ch > 2:
                self._canvas.create_image(cw // 2, ch // 2, image=self._photo)
        else:
            self._draw_placeholder()

    def _draw_frame(self, img: Image.Image, cache: bool = True) -> None:
        try:
            cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        except tk.TclError:
            return
        if cw < 2 or ch < 2:
            return
        if cache:
            try:
                self._pil_cache = img.copy()
            except Exception:
                self._pil_cache = img
        fw, fh = img.size
        scale = min(cw / fw, ch / fh)
        nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
        if (nw, nh) != (fw, fh):
            img = img.resize((nw, nh), Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(img)
        self._canvas.delete("all")
        self._canvas.create_image(cw // 2, ch // 2, image=self._photo)
        self._frames += 1
        self._foot_var.set(f"#{self._frames} · {fw}x{fh} → {nw}x{nh}")
        self._size_var.set(f"{fw}x{fh}")

    def _draw_placeholder(self) -> None:
        if self._streaming:
            return
        try:
            cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        except tk.TclError:
            return
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
