"""右上角延迟曲线：全场唯一的 signature 元素（pit-wall timing strip）。

布局：一眼三段 —— 眉题 + 巨型毫秒数 / 示波器曲线 / RTT·FPS·抖动小格。
颜色纪律：平时琥珀，>500ms 整卡转红，断流转灰，保证操作手余光就能决策。
"""
from __future__ import annotations

import tkinter as tk

from desktop import theme
from desktop.modules.base_panel import BasePanel
from desktop.telemetry.latency_monitor import WARN_MS


class LatencyPanel(BasePanel):
    def __init__(self, master, monitor, **kwargs) -> None:
        self._monitor = monitor
        super().__init__(master, title="LATENCY · 延迟", **kwargs)

    def build(self) -> None:
        # 眉题行：标题 + 状态点
        head = tk.Frame(self, bg=theme.PANEL)
        head.pack(fill=tk.X, padx=theme.PAD, pady=(theme.PAD, 2))
        self._eyebrow = tk.Label(
            head, text="LATENCY · 延迟", bg=theme.PANEL,
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

        # 巨型数字 + 单位 + p95
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

        # 曲线
        self._canvas = tk.Canvas(self, bg="#0B0F16", highlightthickness=1,
                                 highlightbackground=theme.LINE, height=110)
        self._canvas.pack(fill=tk.X, padx=theme.PAD, pady=4)
        self._canvas.bind("<Configure>", lambda _e: self._draw())

        # 小格：RTT / FPS / 帧龄 / 抖动
        grid = tk.Frame(self, bg=theme.PANEL)
        grid.pack(fill=tk.X, padx=theme.PAD, pady=(0, theme.PAD))
        for i in range(4):
            grid.columnconfigure(i, weight=1)
        self._cells: dict[str, tk.StringVar] = {}
        for i, (key, label) in enumerate(
            [("rtt", "RTT"), ("fps", "FPS"), ("age", "帧龄"), ("jit", "抖动")]
        ):
            cell = tk.Frame(grid, bg=theme.PANEL)
            cell.grid(row=0, column=i, sticky="w")
            tk.Label(cell, text=label, bg=theme.PANEL, fg=theme.FAINT,
                     font=theme.FONT_EYEBROW).pack(anchor="w")
            var = tk.StringVar(value="—")
            self._cells[key] = var
            tk.Label(cell, textvariable=var, bg=theme.PANEL, fg=theme.INK,
                     font=theme.FONT_MONO).pack(anchor="w")

        self.after(500, self._tick)

    # ---------- 刷新 ----------
    def _tick(self) -> None:
        try:
            st = self._monitor.stats()
            self._render_numbers(st)
            self._draw()
        finally:
            self.after(500, self._tick)

    def _render_numbers(self, st: dict) -> None:
        cur = st["current_ms"]
        live = st["live"]
        if not live or cur < 0:
            self._num_var.set("—")
            self._dot.config(fg=theme.FAINT)
            self._state_var.set("无流" if st["count"] == 0 and not live else "断流")
            num_color = theme.FAINT
        elif cur >= WARN_MS:
            self._num_var.set(str(int(cur)))
            self._dot.config(fg=theme.BAD)
            self._state_var.set("延迟高")
            num_color = theme.BAD
        else:
            self._num_var.set(str(int(cur)))
            self._dot.config(fg=theme.OK)
            self._state_var.set("正常")
            num_color = theme.AMBER
        # 直接改巨型数字颜色
        self._num_label.config(fg=num_color)

        p95 = st["p95_ms"]
        self._p95_var.set(f"p95 {int(p95)}" if p95 >= 0 else "p95 —")
        rtt = st["rtt_ms"]
        self._cells["rtt"].set(f"{rtt:.0f}ms" if rtt >= 0 else "—")
        self._cells["fps"].set(f"{st['fps']:.1f}" if live else "—")
        age = st["age_ms"]
        self._cells["age"].set(f"{age:.0f}ms" if age >= 0 else "—")
        self._cells["jit"].set(f"{st['jitter_ms']:.0f}ms" if live else "—")

    # ---------- 曲线 ----------
    def _draw(self) -> None:
        c = self._canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 20 or h < 20:
            return
        c.delete("all")
        pad_l, pad_r, pad_t, pad_b = 6, 6, 8, 14
        iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
        samples = [s for s in self._monitor.samples() if s.e2e_ms >= 0][-120:]
        vmax = 800.0  # 固定量程：0~800ms，500ms 处画阈值线，曲线可比
        y_of = lambda v: pad_t + ih - min(max(v, 0), vmax) / vmax * ih

        # 网格
        for frac in (0.25, 0.5, 0.75):
            y = pad_t + ih * frac
            c.create_line(pad_l, y, w - pad_r, y, fill="#1B2432")
        # 阈值线 500ms
        yt = y_of(WARN_MS)
        c.create_line(pad_l, yt, w - pad_r, yt, fill="#5A3A1A", dash=(4, 3))
        c.create_text(w - pad_r - 2, yt - 7, text="500", fill="#B08D4D",
                      font=("Segoe UI", 8), anchor="e")

        if not samples:
            c.create_text(w // 2, h // 2, text="等待延迟采样…", fill=theme.FAINT,
                          font=("Segoe UI", 9))
            return
        # 曲线：琥珀主线，超阈值段转红（右对齐，最新永远在右边缘）
        n = len(samples)
        step = iw / 119
        xs = [w - pad_r - (n - 1 - i) * step for i in range(n)]
        pts = [(xs[i], y_of(samples[i].e2e_ms)) for i in range(n)]
        # 正常段
        for i in range(1, n):
            bad = samples[i].e2e_ms >= WARN_MS or samples[i - 1].e2e_ms >= WARN_MS
            c.create_line(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1],
                          fill=theme.BAD if bad else theme.AMBER, width=2)
        # 末端圆点
        lx, ly = pts[-1]
        c.create_oval(lx - 3, ly - 3, lx + 3, ly + 3,
                      fill=theme.BAD if samples[-1].e2e_ms >= WARN_MS else theme.AMBER,
                      outline="")
        c.create_text(pad_l + 2, h - 4, text="60s", fill=theme.FAINT,
                      font=("Segoe UI", 8), anchor="sw")
        c.create_text(w - pad_r - 2, h - 4, text="now", fill=theme.FAINT,
                      font=("Segoe UI", 8), anchor="se")
