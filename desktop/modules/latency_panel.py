"""右上角延迟曲线：全场唯一的 signature 元素（pit-wall timing strip）。

巨型毫秒数 = **真实滞后**（``render_age_ms``：当前上屏 AU 的年龄）。
它缺失时显示 ``—`` 并在状态行说明原因，**绝不**回退到 staleness 或
``rtt+fetch`` —— 那两个都不是延迟：一个是"最新包有多新"，一个是本机
8ms 出头的往返，出场率再高也跟 5m 现场的 4~10s 无关。

小格行的语义分工（标签必须说清，否则等于没显示）：
  真实      render_age（= 主数字，重复一次便于余光扫）
  队列      接收队列等待估算 —— 画面卡在积压上的直接读数
  积压/丢   待解码包数 + 累计丢包 —— 卡顿来自积压还是断链的判据
  到达陈旧  now - 最新帧落 server，**非延迟**（满流恒 33ms）
  本机往返  HTTP RTT + 取帧，**非延迟**（不含空中段）

颜色纪律：平时琥珀，>500ms 整卡转红，断流转灰。
"""
from __future__ import annotations

import tkinter as tk

from desktop import theme
from desktop.modules.base_panel import BasePanel
from desktop.telemetry.latency_monitor import WARN_MS

# (key, 标签) —— 3 列 x 3 行；标签里的 * 表示"不是延迟"，见脚注。
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
        super().__init__(master, title="LATENCY · 延迟", **kwargs)

    def build(self) -> None:
        # 眉题行：标题 + 状态点
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

        # 小格：3 列 x 2 行，每格 label 说清自己是什么量
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

        # fps 一行（窄格子放不下，塞进脚注行）
        self._fps_var = tk.StringVar(value="fps —")
        tk.Label(self, textvariable=self._fps_var, bg=theme.PANEL, fg=theme.MUTE,
                 font=theme.FONT_MONO_SM).pack(anchor="w", padx=theme.PAD, pady=(2, 0))
        self._foot_var = tk.StringVar(value=FOOTNOTE)
        tk.Label(self, textvariable=self._foot_var, bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.cjk_font(8)).pack(anchor="w", padx=theme.PAD,
                                              pady=(1, theme.PAD))

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
        # st 的键由 LatencyMonitor.stats() 保证存在；这里仍然用 .get 兜底，
        # 免得面板因为遥测层某个键改名而整卡抛异常（after 回调里抛异常
        # 会静默停止刷新，看板永远冻在最后一帧）。
        cur = st.get("current_ms", -1)
        live = st.get("live", False)
        has_latency = st.get("latency_available", cur >= 0)

        if not live:
            self._num_var.set("—")
            self._num_label.config(fg=theme.FAINT)
            self._dot.config(fg=theme.FAINT)
            self._state_var.set("无信号")
        elif not has_latency or cur < 0:
            # 关键：没有真实延迟就明说没有，而不是塞一个假的进来。
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
        # 积压 = 待解码包数 / 累计丢包。drops 是"画面卡顿来自积压而不是
        # 链路断了"的直接证据，所以两值并排。
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

    # ---------- 曲线 ----------
    def _draw(self) -> None:
        c = self._canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 20 or h < 20:
            return
        c.delete("all")
        pad_l, pad_r, pad_t, pad_b = 6, 6, 8, 14
        iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
        # 只画真实滞后。没有它曲线就是空的 —— 空曲线比一条假曲线诚实，
        # 而且空的时候下方状态行会写明原因。
        samples = [s for s in self._monitor.samples() if s.render_age_ms >= 0][-120:]
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
            c.create_text(w // 2, h // 2, text="无 render_age 上报", fill=theme.FAINT,
                          font=("Segoe UI", 9))
            return
        # 曲线：琥珀主线，超阈值段转红（右对齐，最新永远在右边缘）
        n = len(samples)
        step = iw / 119
        xs = [w - pad_r - (n - 1 - i) * step for i in range(n)]
        pts = [(xs[i], y_of(samples[i].render_age_ms)) for i in range(n)]
        # 正常段
        for i in range(1, n):
            bad = (samples[i].render_age_ms >= WARN_MS
                   or samples[i - 1].render_age_ms >= WARN_MS)
            c.create_line(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1],
                          fill=theme.BAD if bad else theme.AMBER, width=2)
        # 末端圆点
        lx, ly = pts[-1]
        c.create_oval(lx - 3, ly - 3, lx + 3, ly + 3,
                      fill=theme.BAD if samples[-1].render_age_ms >= WARN_MS
                      else theme.AMBER,
                      outline="")
        c.create_text(pad_l + 2, h - 4, text="60s", fill=theme.FAINT,
                      font=("Segoe UI", 8), anchor="sw")
        c.create_text(w - pad_r - 2, h - 4, text="now", fill=theme.FAINT,
                      font=("Segoe UI", 8), anchor="se")