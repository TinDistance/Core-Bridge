"""手柄面板：xinput_gui.py 的 pit-wall 版 —— 操作手的另一只眼睛。

布局（一眼四段，横条，放在主区底部通栏）：
  状态（含 slot/帧率/推送） | 按键灯 | 双摇杆+双扳机 | 16 字节协议视图
颜色纪律：连接绿 / 断开红 / 无 XInput 灰；按键灯亮红与验证程序一致。
数据流：只读 GamepadPusher.snapshot()（20Hz 刷 UI），轮询与 POST 全在后台线程。
"""
from __future__ import annotations

import tkinter as tk

from desktop import theme
from desktop.gamepad import xinput as xi
from desktop.modules.base_panel import BasePanel

_HAT = xi.HAT_NAMES
_BTN13 = [("A", 0x01), ("B", 0x02), ("X", 0x08), ("Y", 0x10),
          ("LB", 0x40), ("RB", 0x80)]


class GamepadPanel(BasePanel):
    def __init__(self, master, pusher=None, **kwargs) -> None:
        self._pusher = pusher
        super().__init__(master, title="GAMEPAD · 手柄", **kwargs)

    def build(self) -> None:
        head = tk.Frame(self, bg=theme.PANEL)
        head.pack(fill=tk.X, padx=theme.PAD, pady=(theme.PAD, 2))
        tk.Label(head, text="GAMEPAD · 手柄", bg=theme.PANEL,
                 fg=theme.FAINT, font=theme.FONT_EYEBROW).pack(side=tk.LEFT)
        self._pill_var = tk.StringVar(value="启动中")
        self._pill = tk.Label(head, textvariable=self._pill_var,
                              bg="#232E42", fg=theme.MUTE,
                              font=theme.FONT_EYEBROW, padx=8, pady=2)
        self._pill.pack(side=tk.RIGHT)
        self._conn_var = tk.StringVar(value="正在尝试连接手柄…")
        tk.Label(head, textvariable=self._conn_var, bg=theme.PANEL,
                 fg=theme.MUTE, font=theme.FONT_MONO_SM).pack(
            side=tk.RIGHT, padx=(0, 8))

        body = tk.Frame(self, bg=theme.PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=theme.PAD, pady=(0, 2))
        for i, w in enumerate((1, 1, 0, 0)):
            body.columnconfigure(i, weight=w)

        # ---- 按键灯 ----
        btnf = tk.Frame(body, bg=theme.PANEL)
        btnf.grid(row=0, column=0, sticky="nw", padx=(0, 10))
        self._lamps: dict[str, tk.Label] = {}
        names = [n for n, _ in xi.LAMPS_13 + xi.LAMPS_14] + ["Share"]
        for i, n in enumerate(names):
            lamp = tk.Label(btnf, text=n, width=6, relief="groove",
                            bg="#232E42", fg=theme.MUTE,
                            font=("Segoe UI", 8, "bold"))
            lamp.grid(row=i // 6, column=i % 6, padx=2, pady=2, sticky="nsew")
            self._lamps[n] = lamp
        self._dp_var = tk.StringVar(value="十字键: --")
        tk.Label(btnf, textvariable=self._dp_var, bg=theme.PANEL,
                 fg=theme.MUTE, font=theme.FONT_MONO_SM).grid(
            row=3, column=0, columnspan=6, sticky="w", pady=(4, 0))

        # ---- 摇杆 ----
        self._sticks: dict[str, tuple] = {}
        for j, name in enumerate(("左摇杆", "右摇杆")):
            f = tk.Frame(body, bg=theme.PANEL)
            f.grid(row=0, column=1 + j, sticky="n", padx=4)
            tk.Label(f, text=name, bg=theme.PANEL, fg=theme.FAINT,
                     font=theme.FONT_EYEBROW).pack()
            cv = tk.Canvas(f, width=120, height=120, bg="#0B0F16",
                           highlightthickness=1, highlightbackground=theme.LINE)
            cv.pack()
            cv.create_line(8, 60, 112, 60, fill="#1B2432")
            cv.create_line(60, 8, 60, 112, fill="#1B2432")
            dot = cv.create_oval(0, 0, 0, 0, fill="#FF5D5D", outline="")
            self._sticks[name] = (cv, dot)

        # ---- 扳机 ----
        trigf = tk.Frame(body, bg=theme.PANEL)
        trigf.grid(row=0, column=3, sticky="n", padx=(10, 0))
        tk.Label(trigf, text="扳机 0~255", bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.FONT_EYEBROW).grid(row=0, column=0, columnspan=2)
        self._trigs: dict[str, tuple] = {}
        for j, name in enumerate(("LT", "RT")):
            c = tk.Frame(trigf, bg=theme.PANEL)
            c.grid(row=1, column=j, padx=4)
            tk.Label(c, text=name, bg=theme.PANEL, fg=theme.MUTE,
                     font=theme.FONT_SMALL).pack()
            cv = tk.Canvas(c, width=34, height=120, bg="#0B0F16",
                           highlightthickness=1, highlightbackground=theme.LINE)
            cv.pack()
            bar = cv.create_rectangle(4, 120, 30, 120, fill="#2a7f2a", outline="")
            txt = cv.create_text(17, 10, text="0", fill=theme.MUTE,
                                 font=("Consolas", 8))
            self._trigs[name] = (cv, bar, txt)

        # ---- 协议视图 ----
        prof = tk.Frame(self, bg="#0B0F16", highlightthickness=1,
                        highlightbackground=theme.LINE)
        prof.pack(fill=tk.X, padx=theme.PAD, pady=(2, theme.PAD))
        self._hex_var = tk.StringVar(value="-- 等待手柄数据 --")
        tk.Label(prof, textvariable=self._hex_var, bg="#0B0F16",
                 fg=theme.INK, font=("Consolas", 11, "bold")).pack(
            anchor="w", padx=8, pady=(4, 0))
        self._detail_var = tk.StringVar(value="")
        tk.Label(prof, textvariable=self._detail_var, bg="#0B0F16",
                 fg=theme.MUTE, font=("Consolas", 8)).pack(
            anchor="w", padx=8, pady=(0, 4))

        if not xi.available():
            self._conn_var.set("本机无 XInput（非 Windows / 缺 DLL），仅显示中位报文")
            self._pill_var.set("无 XInput")
            self._pill.config(fg=theme.MUTE, bg="#232E42")

        self.after(50, self._tick)

    # ---------- 刷新（20Hz，只读 snapshot，不做网络 IO） ----------
    def _tick(self) -> None:
        try:
            if self._pusher is not None:
                self._render(self._pusher.snapshot())
        finally:
            self.after(50, self._tick)

    def _set_lamp(self, name: str, on: bool) -> None:
        lbl = self._lamps.get(name)
        if lbl is None:
            return
        if on:
            lbl.config(bg="#C22F2F", fg="white")
        else:
            lbl.config(bg="#232E42", fg=theme.MUTE)

    def _render(self, snap) -> None:
        w = snap.buttons
        for n, _ in xi.LAMPS_13 + xi.LAMPS_14:
            self._set_lamp(n, False)
        self._set_lamp("Share", False)
        for n, m in xi.LAMPS_13:
            if w & m:
                self._set_lamp(n, True)
        for n, m in xi.LAMPS_14:
            if w & m:
                self._set_lamp(n, True)

        # 摇杆（Y 取反，与验证程序一致：上为正）
        for name, x, y in (("左摇杆", snap.lx, -snap.ly),
                           ("右摇杆", snap.rx, -snap.ry)):
            cv, dot = self._sticks[name]
            cx, cy = 60, 60
            dx = x / 32768 * 50
            dy = y / 32768 * 50
            cv.coords(dot, cx + dx - 5, cy + dy - 5, cx + dx + 5, cy + dy + 5)

        for tname, val in (("LT", snap.lt8), ("RT", snap.rt8)):
            cv, bar, txt = self._trigs[tname]
            h = int(val / 255 * 116)
            cv.coords(bar, 4, 120 - h, 30, 120)
            cv.itemconfig(txt, text=str(val))

        hat = xi.hat_from_buttons(w)
        l3 = bool(w & xi.XINPUT_GAMEPAD_LEFT_THUMB)
        r3 = bool(w & xi.XINPUT_GAMEPAD_RIGHT_THUMB)
        self._dp_var.set(
            f"十字键帽子值: {hat} {_HAT.get(hat, '?')}    L3={l3} R3={r3}")

        parsed = xi.parse_protocol(snap.proto)
        self._hex_var.set(" ".join(
            parsed["raw_hex"][i:i + 2] for i in range(0, 32, 2)))
        b13_names = "".join(n for n, m in _BTN13 if parsed["btn"] & m) or "--"
        self._detail_var.set(
            f"LX={parsed['lx']:5d} LY={parsed['ly']:5d} "
            f"RX={parsed['rx']:5d} RY={parsed['ry']:5d}  "
            f"LT={parsed['lt']:4d} RT={parsed['rt']:4d}  "
            f"hat={parsed['hat']} btn=0x{parsed['btn']:02X}({b13_names}) "
            f"sys=0x{parsed['sys']:02X} share=0x{parsed['share']:02X}  "
            f"{'已推送→/command' if snap.posted else '未推送'}")

        if snap.connected:
            self._conn_var.set(
                f"手柄已连接 (slot {snap.slot}) · 包号 {snap.packet} · {snap.fps:.0f}Hz")
            self._pill_var.set("● 手柄")
            self._pill.config(fg=theme.OK, bg="#14352B")
        elif xi.available():
            self._conn_var.set("未连接手柄 · 已自动重连等待中")
            self._pill_var.set("无手柄")
            self._pill.config(fg=theme.BAD, bg="#3A1E1E")
