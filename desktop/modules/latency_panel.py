"""右上角遥测：延迟大条 + 丢包细条（pit-wall timing strip）。

层级是刻意的：**延迟是全场唯一的「大」元素**（26px 主数字 + 110px 曲线），
丢包只配一个 12px 的数字和一条 40px 的细条。理由是延迟有唯一量纲（ms，用户能
直接判断「能不能玩」），而丢包率是排障量 —— 它只在「画面已经不对」时被人看，
所以不该跟延迟抢视觉权重；但它必须**常驻可见**，因为 NACK 重传能把丢包藏起来：
线上 5% / 最终 0% 是两个完全不同的结论（链路差 vs 恢复层在满负荷），只显示
其中一个都会误导。

口径见 ``desktop/telemetry/loss_window.py`` 的模块 docstring。
"""
from __future__ import annotations

import tkinter as tk

from desktop import theme
from desktop.modules.base_panel import BasePanel
from desktop.telemetry import loss_window as LW
from desktop.telemetry.latency_monitor import WARN_MS

CELLS = (
    ("queue", "队列"),
    ("backlog", "积压/溢出"),
    ("loop", "循环"),
    ("stale", "到达陈旧*"),
    ("local", "本机往返*"),
    ("jitter", "抖动"),
)

LOSS_CELLS = (
    ("wire", "线上丢包"),
    ("final", "最终丢失"),
    ("up", "上游缺口*"),
    ("recovery", "重传救回"),
)

FOOTNOTE = "* 到达陈旧/本机往返都不是延迟；主数字才是"
# 页脚刻意拆成两行短句：300px 宽的侧栏放不下一整句 Tk 不会换行、只会裁掉尾巴
# （裁掉的注解比没有注解更糟 —— 读者会以为口径没限制）。
LOSS_FOOT = ("* 上游=中继侧观测的 K230→server 缺口",
             "中继不做重排，无法分辨「重传补回」")

# 细条的纵轴上限。丢包率是长尾分布：正常 <0.1%，劣化 1~3%，断流前冲到 5%+。
# 固定 3% 上限让「0.03% vs 0.3%」这种真有意义的量级差看得出来；超出就贴顶
# （和延迟条 800ms 封顶同一套处理）。
LOSS_VMAX_PCT = 3.0


def _fmt_ms(v: float) -> str:
    return f"{v:.0f}ms" if v >= 0 else "—"


def _fmt_pct(v: float) -> str:
    """丢包率是小数量级，小数位要跟着量级走：

    - <1%   给 3 位 —— 0.062% 会被 1 位显示成 0.1%，而 0.1% 正好是告警线附近，
             把「远低于阈值」显示成「刚好在阈值上」是最糟的一种错。
    - <10%  给 2 位 —— 3.226% 显示成 3.2% 就丢掉了区分「刚过劣化线」的信息。
    - >=10% 给 1 位 —— 这时小数位已经没有意义了。
    """
    if v < 0:
        return "—"
    if v == 0:
        return "0%"
    if v < 1.0:
        return f"{v:.3f}".rstrip("0").rstrip(".") + "%"
    if v < 10.0:
        return f"{v:.2f}".rstrip("0").rstrip(".") + "%"
    return f"{v:.1f}%"


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

        # ---- 丢包段：细条 + 四格明细（从属层级，见模块 docstring）----
        loss_head = tk.Frame(self, bg=theme.PANEL)
        loss_head.pack(fill=tk.X, padx=theme.PAD, pady=(6, 0))
        tk.Label(loss_head, text="LOSS · 丢包", bg=theme.PANEL,
                 fg=theme.FAINT, font=theme.FONT_EYEBROW).pack(side=tk.LEFT)
        self._loss_num_var = tk.StringVar(value="—")
        self._loss_num = tk.Label(
            loss_head, textvariable=self._loss_num_var, bg=theme.PANEL,
            fg=theme.FAINT, font=theme.FONT_MONO,
        )
        self._loss_num.pack(side=tk.RIGHT)

        self._loss_canvas = tk.Canvas(self, bg="#0B0F16", highlightthickness=1,
                                      highlightbackground=theme.LINE, height=40)
        self._loss_canvas.pack(fill=tk.X, padx=theme.PAD, pady=3)
        self._loss_canvas.bind("<Configure>", lambda _e: self._draw_loss())

        lgrid = tk.Frame(self, bg=theme.PANEL)
        lgrid.pack(fill=tk.X, padx=theme.PAD, pady=(1, 0))
        lgrid.columnconfigure(0, weight=1)
        lgrid.columnconfigure(1, weight=1)
        self._loss_cells: dict[str, tk.StringVar] = {}
        for i, (key, label) in enumerate(LOSS_CELLS):
            row, col = divmod(i, 2)
            cell = tk.Frame(lgrid, bg=theme.PANEL)
            cell.grid(row=row, column=col, sticky="w", pady=1)
            tk.Label(cell, text=label, bg=theme.PANEL, fg=theme.FAINT,
                     font=theme.FONT_EYEBROW).pack(anchor="w")
            var = tk.StringVar(value="—")
            self._loss_cells[key] = var
            tk.Label(cell, textvariable=var, bg=theme.PANEL, fg=theme.INK,
                     font=theme.FONT_MONO).pack(anchor="w")

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
                                               pady=(1, 2))
        self._loss_foot_var = tk.StringVar(value=LOSS_FOOT[0])
        tk.Label(self, textvariable=self._loss_foot_var, bg=theme.PANEL,
                 fg=theme.FAINT, font=theme.cjk_font(8)).pack(
            anchor="w", padx=theme.PAD, pady=(0, 0))
        self._loss_foot2_var = tk.StringVar(value=LOSS_FOOT[1])
        tk.Label(self, textvariable=self._loss_foot2_var, bg=theme.PANEL,
                 fg=theme.FAINT, font=theme.cjk_font(8)).pack(
            anchor="w", padx=theme.PAD, pady=(0, theme.PAD))

        self.after(500, self._tick)

    def _tick(self) -> None:
        if self._closed:
            return
        try:
            st = self._monitor.stats()
            self._render_numbers(st)
            self._render_loss(st.get("loss_window") or {})
            self._draw()
            self._draw_loss()
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

    @staticmethod
    def _probe_fault(st: dict) -> bool:
        """探针是否已经不能产新样本（线程死了，或样本陈旧到不再可信）。"""
        if not st.get("probe_alive", True):
            return True
        return bool(st.get("sample_stale", False))

    def _render_numbers(self, st: dict) -> None:
        cur = st.get("current_ms", -1)
        live = st.get("live", False)
        has_latency = st.get("latency_available", cur >= 0)
        probe_dead = self._probe_fault(st)

        # 探针自身没在采样时，「无信号」是误导：它只是最后一个样本被冻住了。
        # 必须先判这一层，否则线程崩掉会被 forever 读成「K230 没在推流」。
        if probe_dead:
            self._num_var.set("—")
            self._num_label.config(fg=theme.BAD)
            self._dot.config(fg=theme.BAD)
            self._state_var.set("探针中断")
        elif not live:
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
            _fmt_ms(st.get("jitter_ms", -1)) if live and not probe_dead else "—")

        fps = st.get("fps", 0) or 0
        self._fps_var.set(
            f"fps {fps:.1f}" if live and not probe_dead else "fps —")
        src = st.get("source") or ""
        foot = FOOTNOTE + (f" · {src}" if src else "")
        if probe_dead:
            # 把「为什么没有数据」写进界面，而不是让人猜。
            err = (st.get("probe_last_error") or "").strip()
            err = err.splitlines()[0][:80] if err else ""
            n = st.get("probe_error_count", 0)
            tail = f"探针异常 {n} 次" if n else "采样线程未运行"
            foot = f"! {tail}{('：' + err) if err else ''}"
        self._foot_var.set(foot)

    def _render_loss(self, win: dict) -> None:
        """丢包段渲染。主数字用**最终**口径（打到画面上的损伤）。

        为什么不拿线上口径当主数字：线上高、最终低说明恢复层在工作，画面是好的；
        这时把 5% 显示成主数字会让人去查一个根本不存在的问题。线上口径放在
        「线上丢包」格里，需要时看得到，但不抢主位。
        """
        final_pct = win.get("final_pct", LW.UNKNOWN)
        wire_pct = win.get("wire_pct", LW.UNKNOWN)

        if final_pct < 0:
            self._loss_num_var.set("—")
            self._loss_num.config(fg=theme.FAINT)
        else:
            self._loss_num_var.set(_fmt_pct(final_pct))
            if final_pct >= LW.BAD_PCT:
                self._loss_num.config(fg=theme.BAD)
            elif final_pct >= LW.WARN_PCT:
                self._loss_num.config(fg=theme.AMBER)
            else:
                self._loss_num.config(fg=theme.OK)

        self._loss_cells["wire"].set(_fmt_pct(wire_pct))
        self._loss_cells["final"].set(_fmt_pct(final_pct))
        self._loss_cells["up"].set(_fmt_pct(win.get("up_pct", LW.UNKNOWN)))
        rec = win.get("recovery_pct", LW.UNKNOWN)
        self._loss_cells["recovery"].set(
            f"{rec:.0f}%" if rec >= 0 else "—")

        # 窗口不满 10 s 时必须说出来，否则启动后前几秒的「0.000%」会被读成
        # 「链路没问题」，而实际只是样本还不够。挂在第一行尾部（仍在一行内）。
        span = win.get("span_s", 0.0) or 0.0
        covered = win.get("covered", False)
        self._loss_foot_var.set(
            LOSS_FOOT[0] if covered else f"{LOSS_FOOT[0]} · 窗口 {span:.0f}s")

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

    def _draw_loss(self) -> None:
        """丢包细条：最终口径的窗口值，1 s 一桶。

        断线而不是连 0 —— 桶内没数据画成 0% 会让「没测到」看起来像「没丢过」，
        这类图最常见的撒谎方式。同理，最终口径的段在丢包 >0 时用 BAD 色，一眼
        能看出「有过损伤」；线上口径不进这条线（它常常是锯齿状的噪声，画出来
        会让人一直以为在坏）。
        """
        c = self._loss_canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 20 or h < 20:
            return
        c.delete("all")
        pad_l, pad_r, pad_t, pad_b = 6, 6, 4, 4
        iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
        y_of = lambda v: pad_t + ih - min(max(v, 0.0), LOSS_VMAX_PCT) \
            / LOSS_VMAX_PCT * ih

        for frac in (0.5,):
            y = pad_t + ih * frac
            c.create_line(pad_l, y, w - pad_r, y, fill="#1B2432")
        yw = y_of(LW.WARN_PCT)
        c.create_line(pad_l, yw, w - pad_r, yw, fill="#5A3A1A", dash=(3, 3))
        c.create_text(pad_l + 2, yw - 6,
                      text=f"{LW.WARN_PCT:g}%", fill="#B08D4D",
                      font=("Segoe UI", 8), anchor="w")

        pts = LW.series(self._monitor.samples(), span_s=LW.SPAN_S,
                         bucket_s=LW.BUCKET_S, metric="final")
        if not pts:
            c.create_text(w // 2, h // 2, text="无丢包上报", fill=theme.FAINT,
                          font=("Segoe UI", 9))
            return
        n = len(pts)
        step = iw / max(1, n - 1)
        xs = [w - pad_r - (n - 1 - i) * step for i in range(n)]
        # 分段画：桶值 UNKNOWN 时断开
        run: list[tuple[int, int]] = []
        for i, (_t, v) in enumerate(pts):
            if v < 0:
                if len(run) > 1:
                    self._plot_run(c, pts, xs, run, y_of)
                run = []
                continue
            run.append(i)
        if len(run) > 1:
            self._plot_run(c, pts, xs, run, y_of)
        elif n == 1 and pts[0][1] >= 0:
            c.create_oval(xs[0] - 2, y_of(pts[0][1]) - 2,
                          xs[0] + 2, y_of(pts[0][1]) + 2,
                          fill=theme.OK, outline="")

    @staticmethod
    def _plot_run(c, pts, xs, run, y_of) -> None:
        """画一段连续有数据的桶。无丢包的段用 OK 色而不是 AMBER —— 零丢包的
        曲线不该看起来像警告。"""
        lost_any = any(pts[i][1] > 0 for i in run)
        color = theme.BAD if lost_any else theme.OK
        for k in range(1, len(run)):
            i0, i1 = run[k - 1], run[k]
            c.create_line(xs[i0], y_of(pts[i0][1]), xs[i1], y_of(pts[i1][1]),
                          fill=color, width=2)
        lx, ly = xs[run[-1]], y_of(pts[run[-1]][1])
        c.create_oval(lx - 2.5, ly - 2.5, lx + 2.5, ly + 2.5,
                      fill=color, outline="")