"""操作台壳：顶栏、主视频、右侧控制与健康、底栏日志。"""
from __future__ import annotations

import sys
import threading
import tkinter as tk

from desktop import theme
from desktop.boot import ServerBoot
from desktop.gamepad.pusher import GamepadPusher
from desktop.modules.gamepad_panel import GamepadPanel
from desktop.modules.header_bar import HeaderBar, format_percent, format_tier
from desktop.modules.latency_panel import LatencyPanel
from desktop.modules.log_panel import LogPanel
from desktop.modules.status_bar import StatusBar
from desktop.modules.stream_panel import StreamPanel
from desktop.server_manager import ServerManager
from desktop.telemetry.latency_monitor import WARN_MS, LatencyMonitor
from desktop.telemetry.loss_window import BAD_PCT, WARN_PCT

_SASH_W = 6
_LOG_FOLD_H = 32
_LOG_OPEN_H = 160
_LOG_FOLD_MIN = 32
_LOG_OPEN_MIN = 120


def _split(master: tk.Misc, orient: str) -> tk.PanedWindow:
    return tk.PanedWindow(
        master, orient=orient, sashwidth=_SASH_W, sashrelief=tk.FLAT,
        bg=theme.LINE, opaqueresize=True, bd=0,
    )


class DesktopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        theme.apply_theme(self)
        self.configure(bg=theme.BG)
        self.title("TinDistance")
        self.geometry("1280x880")
        self.minsize(960, 680)
        self._closed = False
        self._booting = False
        self._server: ServerManager | None = None
        self._monitor: LatencyMonitor | None = None
        self._gamepad: GamepadPusher | None = None
        self.stream_panel: StreamPanel | None = None
        self.gamepad_panel: GamepadPanel | None = None
        self.latency_panel: LatencyPanel | None = None
        self.log_panel: LogPanel | None = None

        self._header = HeaderBar(self, on_release_estop=self._release_estop)
        self._header.pack(fill=tk.X)
        self._header.set_connection("正在启动", "mute")

        self._outer = _split(self, tk.VERTICAL)
        self._outer.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self._main = _split(self._outer, tk.HORIZONTAL)
        self._video_parent = tk.Frame(self._main, bg=theme.BG)
        self._right = _split(self._main, tk.VERTICAL)
        self._gamepad_parent = tk.Frame(self._right, bg=theme.BG)
        self._health_parent = tk.Frame(self._right, bg=theme.BG)
        self._main.add(self._video_parent, minsize=420, stretch="always")
        self._main.add(self._right, minsize=260, stretch="never")
        self._right.add(self._gamepad_parent, minsize=160, stretch="never")
        self._right.add(self._health_parent, minsize=140, stretch="always")
        self._log_parent = tk.Frame(self._outer, bg=theme.BG)
        self._outer.add(self._main, stretch="always")
        self._outer.add(self._log_parent, minsize=_LOG_FOLD_MIN, stretch="never")
        self.after_idle(lambda: self._on_log_fold(True))

        self._status = StatusBar(self, on_retry=self._retry)
        self._status.pack(fill=tk.X)
        self._status.set_status("正在启动服务…")

        self._boot = ServerBoot()
        self._begin_boot(8000)
        self.after(200, self._poll_boot)
        self.after(500, self._tick_header)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _begin_boot(self, port: int) -> None:
        self._booting = True
        self._header.set_connection("正在启动", "mute")
        self._status.show_retry(False)
        self._boot.start(port, self._on_ready, self._on_failed)

    def _retry(self, port: int) -> None:
        if self._booting:
            return
        self._status.set_status("正在启动服务…")
        self._begin_boot(port)

    def _poll_boot(self) -> None:
        if self._closed:
            return
        item = self._boot.poll()
        if item is None:
            self.after(200, self._poll_boot)
            return
        manager, ok, error = item
        self._booting = False
        if self._closed:
            if manager is not None:
                threading.Thread(target=manager.stop, daemon=True).start()
            return
        if ok and manager is not None:
            self._on_ready(manager)
        else:
            if manager is not None:
                threading.Thread(target=manager.stop, daemon=True).start()
            self._on_failed(error or "启动失败")

    def _on_ready(self, manager: ServerManager) -> None:
        self._server = manager
        base_url = manager.base_url
        self._status.set_status("服务已就绪")
        self._status.show_retry(False)

        self._monitor = LatencyMonitor(get_base_url=lambda: base_url)
        self._monitor.start()
        self._gamepad = GamepadPusher(get_base_url=lambda: base_url)
        self._gamepad.start()

        self.stream_panel = StreamPanel(
            self._video_parent, default_server=base_url, monitor=self._monitor)
        self.stream_panel.pack(fill=tk.BOTH, expand=True)

        self.gamepad_panel = GamepadPanel(
            self._gamepad_parent, pusher=self._gamepad, get_base_url=lambda: base_url)
        self.gamepad_panel.pack(fill=tk.BOTH, expand=True)

        self.latency_panel = LatencyPanel(self._health_parent, monitor=self._monitor)
        self.latency_panel.pack(fill=tk.BOTH, expand=True)

        self.log_panel = LogPanel(
            self._log_parent, default_server=base_url, on_fold=self._on_log_fold)
        self.log_panel.pack(fill=tk.BOTH, expand=True)
        self.after_idle(lambda: self._on_log_fold(True))

    def _on_log_fold(self, folded: bool) -> None:
        if folded:
            self._place_log_sash(_LOG_FOLD_H, _LOG_FOLD_MIN)
        else:
            self._place_log_sash(_LOG_OPEN_H, _LOG_OPEN_MIN)

    def _place_log_sash(self, height: int, minsize: int, retry: bool = True) -> None:
        if self._closed:
            return
        try:
            self._outer.paneconfigure(self._log_parent, minsize=minsize)
            self.update_idletasks()
            total = self._outer.winfo_height()
            if total <= height + _SASH_W:
                if retry:
                    self.after(50, lambda: self._place_log_sash(height, minsize, False))
                return
            self._outer.sash_place(0, 0, total - height - _SASH_W)
        except tk.TclError:
            return

    def _on_failed(self, error: str) -> None:
        self._header.set_connection("未连接", "bad")
        self._status.set_status(error or "启动失败")
        self._status.show_retry(True)

    def _release_estop(self) -> None:
        if self._gamepad is not None:
            self._gamepad.clear_estop()

    def _tick_header(self) -> None:
        if self._closed:
            return
        try:
            self._paint_header()
        except tk.TclError:
            return
        finally:
            if not self._closed:
                try:
                    self.after(500, self._tick_header)
                except tk.TclError:
                    pass

    def _paint_header(self) -> None:
        if self._booting:
            self._header.set_connection("正在启动", "mute")
        elif self._server is None:
            self._header.set_connection("未连接", "bad")

        size = (0, 0)
        signal = "wait"
        panel = self.stream_panel
        if panel is not None:
            source_size = getattr(panel, "source_size", None)
            if callable(source_size):
                size = source_size()
            signal_state = getattr(panel, "signal_state", None)
            if callable(signal_state):
                signal = signal_state()
        self._header.set_tier(format_tier(int(size[0]), int(size[1])))

        monitor = self._monitor
        if monitor is None:
            self._header.set_latency("—", "mute")
            self._header.set_rtt("—")
            self._header.set_fps("—")
            self._header.set_loss("—", "mute")
            self._paint_estop()
            return

        st = monitor.stats()
        fault = LatencyPanel._probe_fault(st)
        live = bool(st.get("live"))
        if self._server is not None:
            if fault:
                self._header.set_connection("探针中断", "bad")
            elif signal == "wait":
                self._header.set_connection("在线 · 无画面", "mute")
            elif signal == "reconnect":
                self._header.set_connection("在线 · 重连", "warn")
            else:
                self._header.set_connection("在线 · 有画面", "ok")

        current = st.get("current_ms", -1)
        if fault:
            self._header.set_latency("—", "bad")
        elif (not live) or current < 0:
            self._header.set_latency("—", "mute")
        else:
            level = "bad" if current >= WARN_MS else "ok"
            self._header.set_latency(f"{int(current)} ms", level)

        rtt = st.get("rtt_ms", -1)
        self._header.set_rtt(f"{int(rtt)} ms" if rtt >= 0 else "—")
        fps = st.get("fps", 0)
        self._header.set_fps("—" if fault or not live else f"{fps:.1f}")
        self._header.set_loss(*_loss_text(st))
        self._paint_estop()
        self._paint_status(st)

    def _paint_estop(self) -> None:
        latched = False
        progress = 0.0
        if self._gamepad is not None:
            snap = self._gamepad.snapshot()
            tank = snap.tank or {}
            latched = int(tank.get("estop", 0) or 0) == 1
            progress = float(snap.estop_progress or 0.0)
        self._header.set_estop(latched, progress)

    def _paint_status(self, st: dict) -> None:
        source = st.get("source") or "rtp:8002"
        live = bool(st.get("live"))
        current = st.get("current_ms", -1)
        if not live:
            text = f"{source} · 等待 K230 推流"
        elif current < 0:
            text = (f"{source} · fps {st.get('fps', 0):.1f}"
                    f" · 帧 #{st.get('frame_id', -1)} · 无滞后上报")
        else:
            text = f"{source} · 帧 #{st.get('frame_id', -1)}"
            p95 = st.get("p95_ms", -1)
            if p95 >= 0:
                text += f" · p95 {p95:.0f} ms"
            drops = st.get("drops", -1)
            if drops >= 0:
                text += f" · 溢出丢 {drops:.0f}"
        if self._server is not None and not self._booting:
            self._status.set_status(text)

    def _on_close(self) -> None:
        self._closed = True
        self._boot.cancel()
        pending = self._boot.poll()
        if pending and pending[0] is not None and pending[0] is not self._server:
            threading.Thread(target=pending[0].stop, daemon=True).start()
        try:
            if self._monitor is not None:
                self._monitor.stop()
            if self._gamepad is not None:
                self._gamepad.stop()
            for panel in (self.stream_panel, self.log_panel,
                          self.latency_panel, self.gamepad_panel):
                try:
                    if panel is None:
                        continue
                    if hasattr(panel, "stop"):
                        panel.stop()
                    panel.destroy()
                except Exception:
                    pass
        finally:
            server = self._server
            if server is not None:
                if self._booting:
                    threading.Thread(target=server.stop, daemon=True).start()
                else:
                    server.stop()
            try:
                self.destroy()
            except tk.TclError:
                pass


def _loss_text(st: dict) -> tuple[str, str]:
    window = st.get("loss_window") or {}
    final = window.get("final_pct", -1)
    covered = bool(window.get("covered", True))
    if final < 0:
        return "—", "mute"
    text = format_percent(final)
    if not covered:
        text += " ·未满"
    if final >= BAD_PCT:
        return text, "bad"
    if final >= WARN_PCT:
        return text, "warn"
    if not covered:
        return text, "mute"
    return text, "ok"


def main() -> None:
    theme.enable_dpi_awareness()
    _high_precision_timer(True)
    try:
        app = DesktopApp()
        app.mainloop()
    finally:
        _high_precision_timer(False)


def _high_precision_timer(on: bool) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        fn = ctypes.windll.winmm.timeBeginPeriod if on \
            else ctypes.windll.winmm.timeEndPeriod
        return int(fn(1)) == 0
    except Exception:
        return False


if __name__ == "__main__":
    main()
