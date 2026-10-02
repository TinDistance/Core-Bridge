"""主窗口：pit-wall 三段式 —— 顶栏 / 左图传右遥测 / 手柄条 / 底状态条。

布局（ASCII）：
+---------------------------------------------------------------+
| TINDISTANCE·PIT-WALL   server:http://..  [●LIVE] [132ms]       | 顶栏
+--------------------------------------+------------------------+
|                                      | LATENCY · 延迟         | 右上：曲线
|        VIDEO · 图传 (weight 3)       | ~amber curve~ 132ms    |
|                                      +------------------------+
|                                      | LOGS · 日志            | 右下
+--------------------------------------+------------------------+
| GAMEPAD · 手柄  [●手柄]  按键灯 / 双摇杆 / 扳机 / 16字节协议   | 通栏手柄条
+---------------------------------------------------------------+
| udp:8001 · K230→server · fps 12 · 帧 #1234                     | 底栏
+---------------------------------------------------------------+
手柄链路：XInput ~100Hz 轮询 -> 16 字节 HID -> POST /command，
应用启动即自动连接手柄（无柄则后台重连等待，不卡 UI）。
"""
from __future__ import annotations

import tkinter as tk

from desktop import theme
from desktop.gamepad.pusher import GamepadPusher
from desktop.modules.gamepad_panel import GamepadPanel
from desktop.modules.latency_panel import LatencyPanel
from desktop.modules.log_panel import LogPanel
from desktop.modules.stream_panel import StreamPanel
from desktop.server_manager import ServerManager
from desktop.start_screen import StartScreen
from desktop.telemetry.latency_monitor import LatencyMonitor


class DesktopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        theme.apply_theme(self)
        self.configure(bg=theme.BG)
        self.title("TinDistance · Core-Bridge")
        self.geometry("1280x880")
        self.minsize(960, 680)

        self._server: ServerManager | None = None
        self._monitor: LatencyMonitor | None = None
        self._gamepad: GamepadPusher | None = None
        self._start_screen: StartScreen | None = None

        self._show_start_screen()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _show_start_screen(self) -> None:
        self._start_screen = StartScreen(self, on_ready=self._on_server_ready)
        self._start_screen.pack(fill=tk.BOTH, expand=True)

    def _on_server_ready(self, server: ServerManager) -> None:
        self._server = server
        if self._start_screen is not None:
            self._start_screen.destroy()
            self._start_screen = None
        self._build_main()

    def _build_main(self) -> None:
        assert self._server is not None
        base_url = self._server.base_url

        self._monitor = LatencyMonitor(get_base_url=lambda: base_url)
        self._monitor.start()

        # 手柄：应用启动即尝试连接，后台 ~100Hz 轮询 + 实时 POST /command
        self._gamepad = GamepadPusher(get_base_url=lambda: base_url)
        self._gamepad.start()

        # ---- 顶栏 ----
        header = tk.Frame(self, bg=theme.PANEL, highlightthickness=1,
                          highlightbackground=theme.LINE)
        header.pack(fill=tk.X)
        tk.Label(header, text="TINDISTANCE · PIT-WALL", bg=theme.PANEL,
                 fg=theme.INK, font=("Segoe UI Semibold", 11)).pack(
            side=tk.LEFT, padx=(12, 8), pady=8)
        tk.Label(header, text="Core-Bridge", bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.FONT_SMALL).pack(side=tk.LEFT, pady=8)
        self._hdr_lat_var = tk.StringVar(value="— ms")
        tk.Label(header, textvariable=self._hdr_lat_var, bg=theme.PANEL,
                 fg=theme.AMBER, font=("Consolas", 11, "bold")).pack(
            side=tk.RIGHT, padx=(0, 12), pady=8)
        self._hdr_pill_var = tk.StringVar(value="启动中")
        self._hdr_pill = tk.Label(header, textvariable=self._hdr_pill_var,
                                   bg="#232E42", fg=theme.MUTE,
                                   font=theme.FONT_EYEBROW, padx=8, pady=2)
        self._hdr_pill.pack(side=tk.RIGHT, padx=(0, 8), pady=8)
        tk.Label(header, text=base_url, bg=theme.PANEL, fg=theme.MUTE,
                 font=theme.FONT_MONO_SM).pack(side=tk.RIGHT, padx=(0, 8), pady=8)

        # ---- 主区 ----
        main = tk.Frame(self, bg=theme.BG)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=1, minsize=300)
        main.rowconfigure(0, weight=1)
        main.rowconfigure(1, weight=0)

        self.stream_panel = StreamPanel(main, default_server=base_url,
                                        monitor=self._monitor)
        self.stream_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        side = tk.Frame(main, bg=theme.BG)
        side.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        side.rowconfigure(0, weight=0)
        side.rowconfigure(1, weight=1)
        side.columnconfigure(0, weight=1)

        self.latency_panel = LatencyPanel(side, monitor=self._monitor)
        self.latency_panel.grid(row=0, column=0, sticky="new", pady=(0, 10))

        self.log_panel = LogPanel(side, default_server=base_url)
        self.log_panel.grid(row=1, column=0, sticky="nsew")

        # 手柄通栏条：xinput_gui 功能的 pit-wall 版（按键/摇杆/扳机/协议视图）
        self.gamepad_panel = GamepadPanel(main, pusher=self._gamepad)
        self.gamepad_panel.grid(row=1, column=0, columnspan=2,
                                sticky="ew", pady=(10, 0))

        # ---- 底栏 ----
        foot = tk.Frame(self, bg=theme.PANEL, highlightthickness=1,
                        highlightbackground=theme.LINE)
        foot.pack(fill=tk.X)
        self._foot_var = tk.StringVar(value="udp:8001 · 等待 K230 推流…")
        tk.Label(foot, textvariable=self._foot_var, bg=theme.PANEL,
                 fg=theme.MUTE, font=theme.FONT_MONO_SM).pack(
            side=tk.LEFT, padx=12, pady=5)

        self.after(500, self._tick_chrome)

    def _tick_chrome(self) -> None:
        try:
            if self._monitor is not None:
                st = self._monitor.stats()
                cur = st["current_ms"]
                live = st["live"]
                if not live or cur < 0:
                    self._hdr_lat_var.set("— ms")
                    self._hdr_pill_var.set("无信号")
                    self._hdr_pill.config(fg=theme.MUTE, bg="#232E42")
                    self._hdr_lat_var.set("— ms")
                    self._foot_var.set("udp:8001 · 等待 K230 推流…")
                else:
                    self._hdr_lat_var.set(f"{int(cur)} ms")
                    if cur >= 500:
                        self._hdr_pill_var.set("延迟高")
                        self._hdr_pill.config(fg=theme.BAD, bg="#3A1E1E")
                    else:
                        self._hdr_pill_var.set("● LIVE")
                        self._hdr_pill.config(fg=theme.OK, bg="#14352B")
                    self._foot_var.set(
                        f"udp:8001 · K230→server · fps {st['fps']:.1f} "
                        f"· 帧 #{st['frame_id']} · p95 {int(st['p95_ms'])}ms"
                        if st["p95_ms"] >= 0 else
                        f"udp:8001 · K230→server · fps {st['fps']:.1f} · 帧 #{st['frame_id']}"
                    )
        finally:
            self.after(500, self._tick_chrome)

    def _on_close(self) -> None:
        try:
            if self._monitor is not None:
                self._monitor.stop()
            if self._gamepad is not None:
                self._gamepad.stop()
        finally:
            if self._server is not None:
                self._server.stop()
            self.destroy()


def main() -> None:
    theme.enable_dpi_awareness()  # 必须在 Tk() 之前，否则高分屏下全窗口发虚
    app = DesktopApp()
    app.mainloop()


if __name__ == "__main__":
    main()
