"""健康卡：短状态、四格丢包，以及滞后 / 丢包曲线。"""
from __future__ import annotations

import tkinter as tk

from desktop import theme
from desktop.modules.base_panel import BasePanel
from desktop.telemetry.latency_monitor import WARN_MS
from desktop.telemetry.loss_window import BAD_PCT, WARN_PCT, series

# 曲线高度跟着健康卡剩余空间走，落在这个区间里。坐标变换读画布实时宽高。
_CHART_MIN = 90
_CHART_MAX = 140


def format_percent(value: float) -> str:
    if value < 0:
        return "—"
    if value == 0:
        return "0%"
    if value < 1:
        return f"{value:.3f}".rstrip("0").rstrip(".") + "%"
    if value < 10:
        return f"{value:.2f}".rstrip("0").rstrip(".") + "%"
    return f"{value:.1f}".rstrip("0").rstrip(".") + "%"


class LatencyPanel(BasePanel):
    def __init__(self, master, monitor, **kwargs) -> None:
        self._monitor = monitor
        self._closed = False
        self._chart_px = 0
        self._fitting = False
        super().__init__(master, title="健康", **kwargs)

    def build(self) -> None:
        self._head = tk.Frame(self, bg=theme.PANEL)
        self._head.pack(fill=tk.X, padx=theme.PAD, pady=(theme.PAD, 4))
        tk.Label(self._head, text="健康", bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.FONT_EYEBROW).pack(side=tk.LEFT)
        self._state = tk.StringVar(value="—")
        self._state_label = tk.Label(
            self, textvariable=self._state, bg=theme.PANEL, fg=theme.MUTE,
            font=theme.cjk_font(10), wraplength=280, justify=tk.LEFT,
        )
        self._state_label.pack(fill=tk.X, padx=theme.PAD)

        self._grid = tk.Frame(self, bg=theme.PANEL)
        self._grid.pack(fill=tk.X, padx=theme.PAD, pady=(8, 4))
        self._cells: dict[str, tk.Label] = {}
        for col, (key, title) in enumerate((
            ("wire", "线上"),
            ("final", "最终"),
            ("up", "上游"),
            ("recovery", "救回"),
        )):
            box = tk.Frame(self._grid, bg=theme.PANEL)
            box.grid(row=0, column=col, sticky="ew", padx=2)
            self._grid.columnconfigure(col, weight=1)
            tk.Label(box, text=title, bg=theme.PANEL, fg=theme.FAINT,
                     font=theme.FONT_EYEBROW).pack(anchor="w")
            value = tk.Label(box, text="—", bg=theme.PANEL, fg=theme.INK,
                             font=theme.FONT_MONO)
            value.pack(anchor="w")
            self._cells[key] = value

        self._chart = tk.Canvas(
            self, bg=theme.PANEL_2, height=110, highlightthickness=0,
        )
        self._chart.pack(fill=tk.X, padx=theme.PAD, pady=(6, theme.PAD))
        self.bind("<Configure>", self._fit_chart)
        self._chart.bind("<Configure>", self._on_chart_configure)
        self.after(500, self._tick)

    @staticmethod
    def _probe_fault(st: dict) -> bool:
        if not st.get("probe_alive", True):
            return True
        return bool(st.get("sample_stale", False))

    def _tick(self) -> None:
        if self._closed:
            return
        try:
            self._render(self._monitor.stats())
            self._draw_chart(self._monitor.samples())
        except tk.TclError:
            return
        finally:
            if not self._closed:
                try:
                    self.after(500, self._tick)
                except tk.TclError:
                    pass

    def _render(self, st: dict) -> None:
        current = st.get("current_ms", -1)
        live = bool(st.get("live"))
        has_latency = st.get("latency_available", current >= 0)
        if self._probe_fault(st):
            err = str(st.get("probe_last_error") or "").splitlines()
            first = (err[0] if err else "")[:80]
            count = st.get("probe_error_count", 0)
            text = "探针中断"
            if first:
                text += f" · {first}"
            if count:
                text += f" · {count}"
            self._state.set(text)
            self._state_label.config(fg=theme.BAD)
        elif not live:
            self._state.set("无信号")
            self._state_label.config(fg=theme.MUTE)
        elif not has_latency or current < 0:
            self._state.set("无滞后")
            self._state_label.config(fg=theme.MUTE)
        elif current >= WARN_MS:
            self._state.set("滞后高")
            self._state_label.config(fg=theme.BAD)
        else:
            self._state.set("正常")
            self._state_label.config(fg=theme.OK)

        window = st.get("loss_window") or {}
        final = window.get("final_pct", -1)
        covered = bool(window.get("covered", True))
        self._set_cell("wire", format_percent(window.get("wire_pct", -1)), theme.INK)
        level = theme.MUTE
        if final >= BAD_PCT:
            level = theme.BAD
        elif final >= WARN_PCT:
            level = theme.WARN
        elif final >= 0 and covered:
            level = theme.OK
        self._set_cell("final", format_percent(final), level)
        self._set_cell("up", format_percent(window.get("up_pct", -1)), theme.INK)
        recovery = window.get("recovery_pct", -1)
        if recovery >= 0:
            self._set_cell("recovery", f"{int(round(recovery))}%", theme.INK)
        else:
            self._set_cell("recovery", "—", theme.MUTE)

    def _set_cell(self, key: str, text: str, color: str) -> None:
        self._cells[key].config(text=text, fg=color)

    def _fit_chart(self, event=None) -> None:
        """曲线高度取父卡扣除文字后的剩余，夹在约 90–140。"""
        if self._closed or self._fitting:
            return
        if event is not None and event.widget is not self:
            return
        try:
            ph = self.winfo_height()
        except tk.TclError:
            return
        if ph < 2:
            return
        chrome = theme.PAD + 4 + 8 + 4 + 6 + theme.PAD
        for w in (self._head, self._state_label, self._grid):
            try:
                chrome += w.winfo_height()
            except tk.TclError:
                return
        avail = ph - chrome
        if avail < _CHART_MIN:
            h = max(1, int(avail))
        else:
            h = min(_CHART_MAX, int(avail))
        if h < 2 or h == self._chart_px:
            return
        self._fitting = True
        try:
            self._chart.configure(height=h)
            self._chart_px = h
        finally:
            self._fitting = False
        try:
            self._draw_chart(self._monitor.samples())
        except tk.TclError:
            return

    def _on_chart_configure(self, _event=None) -> None:
        if self._closed or self._fitting:
            return
        try:
            self._draw_chart(self._monitor.samples())
        except tk.TclError:
            return

    def _draw_chart(self, samples) -> None:
        c = self._chart
        try:
            cw, ch = c.winfo_width(), c.winfo_height()
        except tk.TclError:
            return
        if cw < 4 or ch < 4:
            return
        c.delete("all")
        pad_x = max(2, int(cw * 0.02))
        pad_y = max(2, int(ch * 0.06))
        gap = max(2, int(ch * 0.05))
        usable = ch - pad_y * 2 - gap
        if usable < 4:
            return
        band = usable / 2.0
        x0, x1 = pad_x, max(pad_x + 2, cw - pad_x)
        lat_top = float(pad_y)
        lat_bot = lat_top + band
        loss_top = lat_bot + gap
        loss_bot = loss_top + band

        lat = [(float(s.t), float(s.render_age_ms)) for s in samples]
        lat_hi = WARN_MS
        for _t, v in lat:
            if v > lat_hi:
                lat_hi = v
        self._baseline(c, x0, x1, lat_bot)
        self._stroke(c, lat, x0, x1, lat_top, lat_bot, lat_hi, theme.AMBER)

        wire = series(samples, metric="wire")
        final = series(samples, metric="final")
        loss_hi = BAD_PCT
        for seq in (wire, final):
            for _t, v in seq:
                if v > loss_hi:
                    loss_hi = v
        self._baseline(c, x0, x1, loss_bot)
        # 线上在下，最终盖在上面；< 0 的桶在 _stroke 里断开。
        self._stroke(c, wire, x0, x1, loss_top, loss_bot, loss_hi, theme.MUTE)
        self._stroke(c, final, x0, x1, loss_top, loss_bot, loss_hi, theme.BAD)

    @staticmethod
    def _baseline(canvas: tk.Canvas, x0: float, x1: float, y: float) -> None:
        canvas.create_line(x0, y, x1, y, fill=theme.LINE)

    @staticmethod
    def _stroke(canvas: tk.Canvas, pts, x0: float, x1: float,
                y_top: float, y_bot: float, ymax: float, color: str) -> None:
        """把 (t, value) 画成折线。value < 0（含 UNKNOWN）断开，不画成 0。"""
        if not pts or x1 <= x0 or y_bot <= y_top:
            return
        t0 = float(pts[0][0])
        t1 = float(pts[-1][0])
        span = t1 - t0
        width = x1 - x0
        height = y_bot - y_top
        if ymax <= 0:
            ymax = 1.0

        def xy(t: float, v: float) -> tuple[float, float]:
            if span <= 0:
                x = (x0 + x1) / 2.0
            else:
                x = x0 + (t - t0) / span * width
            frac = 0.0 if v <= 0 else min(v, ymax) / ymax
            return x, y_bot - frac * height

        seg: list[tuple[float, float]] = []

        def flush() -> None:
            nonlocal seg
            if len(seg) >= 2:
                flat = [n for p in seg for n in p]
                canvas.create_line(
                    *flat, fill=color, width=1.5,
                    capstyle=tk.ROUND, joinstyle=tk.ROUND,
                )
            elif len(seg) == 1:
                x, y = seg[0]
                canvas.create_oval(
                    x - 1.5, y - 1.5, x + 1.5, y + 1.5,
                    outline=color, fill=color, width=1,
                )
            seg = []

        for t, v in pts:
            if v < 0:
                flush()
                continue
            seg.append(xy(float(t), float(v)))
        flush()

    def destroy(self) -> None:
        self._closed = True
        super().destroy()
