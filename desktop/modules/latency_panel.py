"""右上角延迟曲线：全场唯一的 signature 元素（pit-wall timing strip）。"""
from __future__ import annotations

import tkinter as tk

from desktop import theme
from desktop.modules.base_panel import BasePanel
from desktop.telemetry.latency_monitor import WARN_MS

CELLS = (
    ("queue", "队列"),
    ("backlog", "积压/丢"),
    ("loop", "循环"),
    ("stale", "到达陈旧*"),
    ("local", "本机往返*"),
    ("jitter", "抖动"),
)

FOOTNOTE = "* 到达陈旧/本机往返都不是延迟；主数字才是"


def _fmt_ms(v: float) -> str:
    return f"{v:.0f}ms" if v >= 0 else "—"


class LatencyPanel(BasePanel):
    def __init__(self, master, monitor, **kwargs) -> None:
        self._monitor = monitor
        self._closed = False
        super().__init__(master, title="LATENCY · 延迟", **kwargs)

    def build(self) -> None:
        head = tk.Frame(self, bg=theme.PANEL)
        head.pack(fill=tk.X, padx=theme.PAD, pady=(theme.PAD, 2))
        self._eyebrow = tk.Label(
            head, text="真实滞后 · render_age", bg=theme.PANEL,
            fg=theme.FAINT, font=theme.FONT_EYEBROW,
        )
        self._eyebrow.pack(side=tk.LEFT)
        self._dot = tk.Label(head, text="●", bg=theme.PANEL, fg=theme.FAINT, font=("Segoe UI", 10))
        self._dot.pack(side=tk.RIGHT)
        self._state_var = tk.StringVar(value="等待数据…")
        tk.Label(
            head, textvariable=self._state_var, bg=theme.PANEL,
            fg=theme.MUTE, font=theme.FONT_SMALL,
        ).pack(side=tk.RIGHT, padx=(0, 6))

        num_row = tk.Frame(self, bg=theme.PANEL)
        num_row.pack(fill=tk.X, padx=theme.PAD, pady=(0, 2))
        self._num_var = tk.StringVar(value="—")
        self._num_label = tk.Label(
            num_row, textvariable=self._num_var, bg=theme.PANEL,
            fg=theme.AMBER, font=theme.FONT_MONO_BIG,
        )
        self._num_label.pack(side=tk.LEFT)
        tk.Label(num_row, text="ms", bg=theme.PANEL, fg=theme.MUTE, font=("Segoe UI", 11)).pack(
            side=tk.LEFT, padx=(4, 0), pady=(10, 0)
        )
        self._p95_var = tk.StringVar(value="p95 —")
        tk.Label(
            num_row, textvariable=self._p95_var, bg=theme.PANEL,
            fg=theme.MUTE, font=theme.FONT_MONO_SM,
        ).pack(side=tk.RIGHT, pady=(12, 0))

        self._canvas = tk.Canvas(self, bg="#0B0F16", highlightthickness=1,
                                 highlightbackground=theme.LINE, height=110)
        self._canvas.pack(fill=tk.X, padx=theme.PAD, pady=4)
        self._canvas.bind("<Configure>", lambda _e: self._draw())

        grid = tk.Frame(self, bg=theme.PANEL)
        grid.pack(fill=tk.X, padx=theme.PAD, pady=(2, 0))
        for i in range(3):
            grid.columnconfigure(i, weight=1)
        self._cells: dict[str, tk.StringVar] = {}
        for i, (key, label) in enumerate(CELLS):
            row, col = divmod(i, 3)
            cell = tk.Frame(grid, bg=theme.PANEL)
            cell.grid(row=row, column=col, sticky="w", pady=1)
            tk.Label(cell, text=label, bg=theme.PANEL, fg=theme.FAINT,
                     font=theme.FONT_EYEBROW).pack(anchor="w")
            var = tk.StringVar(value="—")
            self._cells[key] = var
            tk.Label(cell, textvariable=var, bg=theme.PANEL, fg=theme.INK,
                     font=theme.FONT_MONO).pack(anchor="w")

        self._fps_var = tk.StringVar(value="fps —")
        tk.Label(self, textvariable=self._fps_var, bg=theme.PANEL, fg=theme.MUTE,
                 font=theme.FONT_MONO_SM).pack(anchor="w", padx=theme.PAD, pady=(2, 0))
        self._foot_var = tk.StringVar(value=FOOTNOTE)
        tk.Label(self, textvariable=self._foot_var, bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.cjk_font(8)).pack(anchor="w", padx=theme.PAD,
                                              pady=(1, theme.PAD))

        self.after(500, self._tick)

    def _tick(self) -> None:
        if self._closed:
            return
        try:
            st = self._monitor.stats()
            self._render_numbers(st)
            self._draw()
        except tk.TclError:
            return
        finally:
            try:
                self.after(500, self._tick)
            except tk.TclError:
                pass

    def destroy(self) -> None:  # type: ignore[override]
        self._closed = True
        super().destroy()

    def _render_numbers(self, st: dict) -> None:
        cur = st.get("current_ms", -1)
        live = st.get("live", False)
        has_latency = st.get("latency_available", cur >= 0)

        if not live:
            self._num_var.set("—")
            self._num_label.config(fg=theme.FAINT)
            self._dot.config(fg=theme.FAINT)
            self._state_var.set("无信号")
        elif not has_latency or cur < 0:
            self._num_var.set("—")
            self._num_label.config(fg=theme.MUTE)
            self._dot.config(fg=theme.MUTE)
            self._state_var.set("无 render_age")
        elif cur >= WARN_MS:
            self._num_var.set(str(int(cur)))
            self._num_label.config(fg=theme.BAD)
            self._dot.config(fg=theme.BAD)
            self._state_var.set("滞后高")
        else:
            self._num_var.set(str(int(cur)))
            self._num_label.config(fg=theme.AMBER)
            self._dot.config(fg=theme.OK)
            self._state_var.set("正常")

        p95 = st.get("p95_ms", -1)
        self._p95_var.set(f"p95 {int(p95)}" if p95 >= 0 else "p95 —")

        queue = st.get("queue_ms", -1)
        self._cells["queue"].set(_fmt_ms(queue))
        pk = st.get("backlog_packets", -1)
        drops = st.get("drops", -1)
        if pk >= 0 or drops >= 0:
            self._cells["backlog"].set(
                f"{int(pk) if pk >= 0 else '—'}p/{int(drops) if drops >= 0 else '—'}")
        else:
            self._cells["backlog"].set("—")
        loop_avg = st.get("loop_us_avg", -1)
        loop_max = st.get("loop_us_max", -1)
        self._cells["loop"].set(
            f"{loop_avg/1000:.1f}/{loop_max/1000:.1f}ms"
            if loop_avg >= 0 and loop_max >= 0 else "—")
        self._cells["stale"].set(_fmt_ms(st.get("staleness_ms", -1)))
        self._cells["local"].set(_fmt_ms(st.get("local_ms", -1)))
        self._cells["jitter"].set(
            _fmt_ms(st.get("jitter_ms", -1)) if live else "—")

        fps = st.get("fps", 0) or 0
        self._fps_var.set(f"fps {fps:.1f}" if live else "fps —")
        src = st.get("source") or ""
        self._foot_var.set(f"{FOOTNOTE}" + (f" · {src}" if src else ""))

    def _draw(self) -> None:
        c = self._canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 20 or h < 20:
            return
        c.delete("all")
        pad_l, pad_r, pad_t, pad_b = 6, 6, 8, 14
        iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
        samples = [s for s in self._monitor.samples() if s.render_age_ms >= 0][-120:]
        vmax = 800.0
        y_of = lambda v: pad_t + ih - min(max(v, 0), vmax) / vmax * ih

        for frac in (0.25, 0.5, 0.75):
            y = pad_t + ih * frac
            c.create_line(pad_l, y, w - pad_r, y, fill="#1B2432")
        yt = y_of(WARN_MS)
        c.create_line(pad_l, yt, w - pad_r, yt, fill="#5A3A1A", dash=(4, 3))
        c.create_text(w - pad_r - 2, yt - 7, text="500", fill="#B08D4D",
                      font=("Segoe UI", 8), anchor="e")

        if not samples:
            c.create_text(w // 2, h // 2, text="无 render_age 上报", fill=theme.FAINT,
                          font=("Segoe UI", 9))
            return
        n = len(samples)
        step = iw / 119
        xs = [w - pad_r - (n - 1 - i) * step for i in range(n)]
        pts = [(xs[i], y_of(samples[i].render_age_ms)) for i in range(n)]
        for i in range(1, n):
            bad = (samples[i].render_age_ms >= WARN_MS
                   or samples[i - 1].render_age_ms >= WARN_MS)
            c.create_line(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1],
                          fill=theme.BAD if bad else theme.AMBER, width=2)
        lx, ly = pts[-1]
        c.create_oval(lx - 3, ly - 3, lx + 3, ly + 3,
                      fill=theme.BAD if samples[-1].render_age_ms >= WARN_MS
                      else theme.AMBER,
                      outline="")
        c.create_text(pad_l + 2, h - 4, text="60s", fill=theme.FAINT,
                      font=("Segoe UI", 8), anchor="sw")
        c.create_text(w - pad_r - 2, h - 4, text="now", fill=theme.FAINT,
                      font=("Segoe UI", 8), anchor="se")