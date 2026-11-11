"""控制卡：摇杆、按键灯、扳机、履带读数和指令链路。"""
from __future__ import annotations

import threading
import time
import tkinter as tk

import httpx

from desktop import theme
from desktop.gamepad import xinput as xi
from desktop.modules.base_panel import BasePanel

UI_TICK_MS = 50
SERVER_POLL_S = 0.5
_STICK_PX = 88

_YAW_TEXT = {0: "停", 1: "右转", 2: "左转"}
_PITCH_TEXT = {0: "停", 1: "上仰", 2: "下俯"}
_REAR_TEXT = {0: "停", 1: "顺时针", 2: "逆时针"}
_OK = "#7FBF9A"
_LEVEL_FG = {"ok": _OK, "warn": theme.WARN, "bad": theme.BAD, "mute": theme.MUTE}

# LAMPS_13 在上，LAMPS_14 在下。掩码只在 buttons 仍是整型位图时使用。
_LAMPS = tuple(xi.LAMPS_13) + tuple(xi.LAMPS_14)


def _axis_u16(v: int) -> int:
    """收成 0..65535，中位 32768。

    规格按这段无符号轴取点。当前快照存的是 XInput 有符号 SHORT
    （中位 0，-32768..32767），先加 32768。已经大于 32767 的值视为
    无符号轴，不再平移。
    """
    v = int(v)
    if v <= 32767:
        v += 32768
    if v < 0:
        return 0
    if v > 65535:
        return 65535
    return v


def _axis_unit(v: int) -> float:
    return max(-1.0, min(1.0, (_axis_u16(v) - 32768) / 32767.0))


def _lamp_on(buttons, name: str, mask: int) -> bool:
    """灯名来自 LAMPS。dict 按灯名；整型位图按同名掩码。"""
    if isinstance(buttons, dict):
        return bool(buttons.get(name))
    try:
        return bool(int(buttons) & int(mask))
    except (TypeError, ValueError):
        return False


def format_server_line(state: dict) -> tuple[str, str]:
    if not state or not state.get("base_url"):
        return "服务端 -- 等待地址", "mute"
    if not state.get("ok"):
        err = (state.get("error") or "连接失败")[:60]
        return f"服务端 ✕ {err}", "bad"
    cmd = state.get("command") or {}
    udp_in = state.get("udp_in") or {}
    udp_out = state.get("udp_out") or {}
    if cmd:
        if cmd.get("connected"):
            cmd_text = (f"服务端● seq={cmd.get('seq', 0)} "
                        f"count={cmd.get('count', 0)} "
                        f"age={cmd.get('age_ms', -1)}ms")
            level = "ok"
        else:
            cmd_text = f"服务端○ 失联 age={cmd.get('age_ms', -1)}ms"
            level = "warn"
    else:
        cmd_text = "服务端○ 无 /command"
        level = "warn"
    rx = udp_in.get("rx", -1)
    tx = udp_in.get("tx", -1)
    endpoint = udp_out.get("endpoint")
    if rx >= 0 or tx >= 0:
        link_text = f"UDP入 rx={rx} tx={tx}"
        bad = int(udp_in.get("dropped_bad", 0) or 0)
        stale = int(udp_in.get("dropped_stale", 0) or 0)
        if bad or stale:
            link_text += f" 丢坏={bad}/乱序={stale}"
    else:
        link_text = "UDP入 --"
    exec_text = f"K230→{endpoint}" if endpoint else "K230端点未学习"
    if not udp_out.get("thread_alive", True):
        exec_text += "（推送线程停）"
        level = "bad" if level == "ok" else level
    return f"{cmd_text} · {link_text} · {exec_text}", level


def fetch_server_state(client: httpx.Client, base_url: str) -> dict:
    state: dict = {"base_url": base_url, "ok": False, "at": time.time()}
    try:
        resp = client.get(f"{base_url}/command", timeout=2.0)
        state["command"] = resp.json() if resp.status_code == 200 else None
    except Exception as e:
        state["error"] = str(e)[:80]
        return state
    try:
        resp = client.get(f"{base_url}/video/rtp_status", timeout=2.0)
        if resp.status_code == 200:
            body = resp.json()
            state["udp_in"] = body.get("command_udp_in")
            state["udp_out"] = body.get("command_udp")
    except Exception:
        pass
    state["ok"] = True
    return state


class GamepadPanel(BasePanel):
    def __init__(self, master, pusher=None, get_base_url=None, **kwargs) -> None:
        self._pusher = pusher
        self._get_base_url = get_base_url
        self._closed = False
        self._server_lock = threading.Lock()
        self._server_state: dict = {}
        self._server_stop = threading.Event()
        self._server_thread: threading.Thread | None = None
        self._lt_val = 0
        self._rt_val = 0
        super().__init__(master, title="控制", **kwargs)

    def build(self) -> None:
        head = tk.Frame(self, bg=theme.PANEL)
        head.pack(fill=tk.X, padx=theme.PAD, pady=(theme.PAD, 4))
        tk.Label(head, text="控制", bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.cjk_font(9)).pack(side=tk.LEFT)
        self._status = tk.Label(head, text="—", bg=theme.PANEL, fg=theme.MUTE,
                                font=theme.cjk_font(9))
        self._status.pack(side=tk.RIGHT)

        sticks = tk.Frame(self, bg=theme.PANEL)
        sticks.pack(fill=tk.X, padx=theme.PAD, pady=(0, 4))
        sticks.columnconfigure(0, weight=1)
        sticks.columnconfigure(1, weight=1)
        self._lx_canvas, self._lx_dot = self._make_stick(sticks)
        self._rx_canvas, self._rx_dot = self._make_stick(sticks)
        self._lx_canvas.grid(row=0, column=0, padx=(0, 4))
        self._rx_canvas.grid(row=0, column=1, padx=(4, 0))

        bars = tk.Frame(self, bg=theme.PANEL)
        bars.pack(fill=tk.X, padx=theme.PAD, pady=(0, 4))
        self._lt_bar = self._make_bar(bars, "LT")
        self._rt_bar = self._make_bar(bars, "RT")
        self._lt_bar.bind("<Configure>", lambda _e: self._paint_bar(self._lt_bar, self._lt_val))
        self._rt_bar.bind("<Configure>", lambda _e: self._paint_bar(self._rt_bar, self._rt_val))

        lamps = tk.Frame(self, bg=theme.PANEL)
        lamps.pack(fill=tk.X, padx=theme.PAD, pady=(0, 4))
        self._lamps: dict[str, tuple[tk.Label, int]] = {}
        cols = 6
        for i, (name, mask) in enumerate(_LAMPS):
            lbl = tk.Label(lamps, text=name, bg=theme.PANEL_2, fg=theme.FAINT,
                           font=theme.cjk_font(8), padx=2, pady=1)
            lbl.grid(row=i // cols, column=i % cols, padx=1, pady=1, sticky="ew")
            self._lamps[name] = (lbl, mask)
        for col in range(cols):
            lamps.columnconfigure(col, weight=1)

        metrics = tk.Frame(self, bg=theme.PANEL)
        metrics.pack(fill=tk.X, padx=theme.PAD, pady=(2, 2))
        self._metric: dict[str, tk.Label] = {}
        captions = ("左履带", "右履带", "炮塔", "后臂", "开火", "急停")
        weights = (1, 1, 2, 1, 1, 1)
        for col, (key, weight) in enumerate(zip(captions, weights)):
            metrics.columnconfigure(col, weight=weight)
            cell = tk.Frame(metrics, bg=theme.PANEL)
            cell.grid(row=0, column=col, sticky="ew", padx=1)
            tk.Label(cell, text=key, bg=theme.PANEL, fg=theme.FAINT,
                     font=theme.cjk_font(8)).pack(anchor="w")
            value = tk.Label(cell, text="—", bg=theme.PANEL, fg=theme.INK,
                             font=theme.cjk_font(9))
            value.pack(anchor="w")
            self._metric[key] = value

        send = tk.Frame(self, bg=theme.PANEL)
        send.pack(fill=tk.X, padx=theme.PAD, pady=(4, 0))
        tk.Label(send, text="发送", bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.cjk_font(8)).pack(side=tk.LEFT, padx=(0, 6))
        self._send_value = tk.Label(send, text="—", anchor="w", bg=theme.PANEL,
                                    fg=theme.MUTE, font=theme.FONT_MONO)
        self._send_value.pack(side=tk.LEFT, fill=tk.X, expand=True)

        cmd = tk.Frame(self, bg=theme.PANEL)
        cmd.pack(fill=tk.X, padx=theme.PAD, pady=(1, theme.PAD))
        tk.Label(cmd, text="指令", bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.cjk_font(8)).pack(side=tk.LEFT, anchor="n", padx=(0, 6))
        self._cmd_value = tk.Label(cmd, text="—", anchor="w", justify=tk.LEFT,
                                   bg=theme.PANEL, fg=theme.MUTE,
                                   font=theme.FONT_MONO, wraplength=220)
        self._cmd_value.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.bind("<Configure>", self._on_configure)

        self._start_server_poll()
        self.after(UI_TICK_MS, self._tick)

    def _make_stick(self, parent: tk.Frame) -> tuple[tk.Canvas, int]:
        size = _STICK_PX
        canvas = tk.Canvas(parent, width=size, height=size, bg=theme.PANEL_2,
                           highlightthickness=1, highlightbackground=theme.LINE,
                           bd=0)
        mid = size / 2
        pad = 6
        canvas.create_oval(pad, pad, size - pad, size - pad, outline=theme.LINE)
        canvas.create_line(mid, pad, mid, size - pad, fill=theme.FAINT)
        canvas.create_line(pad, mid, size - pad, mid, fill=theme.FAINT)
        dot = canvas.create_oval(mid - 4, mid - 4, mid + 4, mid + 4,
                                 fill=theme.INK, outline="")
        return canvas, dot

    def _make_bar(self, parent: tk.Frame, title: str) -> tk.Canvas:
        row = tk.Frame(parent, bg=theme.PANEL)
        row.pack(fill=tk.X, pady=1)
        tk.Label(row, text=title, width=2, anchor="w", bg=theme.PANEL,
                 fg=theme.FAINT, font=theme.FONT_MONO).pack(side=tk.LEFT)
        bar = tk.Canvas(row, height=8, bg=theme.PANEL_2, highlightthickness=0, bd=0)
        bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        return bar

    def _on_configure(self, event) -> None:
        if event.widget is not self or event.width < 40:
            return
        self._cmd_value.configure(wraplength=max(120, event.width - 56))

    def _place_dot(self, canvas: tk.Canvas, dot: int, x: int, y: int) -> None:
        size = _STICK_PX
        mid = size / 2
        radius = size / 2 - 12
        u = _axis_unit(x)
        v = _axis_unit(y)
        px = mid + u * radius
        py = mid - v * radius
        canvas.coords(dot, px - 4, py - 4, px + 4, py + 4)
        canvas.itemconfig(dot, fill=theme.AMBER if abs(u) > 0.08 or abs(v) > 0.08 else theme.INK)

    def _paint_bar(self, canvas: tk.Canvas, value: int) -> None:
        canvas.delete("fill")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width < 2 or height < 2:
            return
        frac = max(0.0, min(1.0, int(value) / 255.0))
        if frac <= 0:
            return
        canvas.create_rectangle(0, 0, max(1, width * frac), height,
                                fill=theme.AMBER, width=0, tags="fill")

    def _set_metric(self, key: str, text: str, color: str) -> None:
        self._metric[key].config(text=text, fg=color)

    def _base_url(self) -> str:
        try:
            return (self._get_base_url() or "").rstrip("/") if self._get_base_url else ""
        except Exception:
            return ""

    def _start_server_poll(self) -> None:
        if self._server_thread is not None and self._server_thread.is_alive():
            return
        self._server_stop.clear()
        self._server_thread = threading.Thread(
            target=self._server_loop, daemon=True, name="gamepad-server-poll")
        self._server_thread.start()

    def _server_loop(self) -> None:
        try:
            with httpx.Client(trust_env=False) as client:
                while not self._server_stop.is_set():
                    base = self._base_url()
                    if base:
                        try:
                            state = fetch_server_state(client, base)
                        except Exception as exc:
                            state = {"base_url": base, "ok": False,
                                     "error": str(exc)[:80], "at": time.time()}
                        with self._server_lock:
                            self._server_state = state
                    if self._server_stop.wait(SERVER_POLL_S):
                        break
        finally:
            self._server_thread = None

    def server_snapshot(self) -> dict:
        with self._server_lock:
            return dict(self._server_state)

    def stop(self) -> None:
        self._server_stop.set()

    def _tick(self) -> None:
        if self._closed:
            return
        try:
            snap = self._pusher.snapshot() if self._pusher is not None else None
            self._render(snap)
        except tk.TclError:
            return
        finally:
            if not self._closed:
                try:
                    self.after(UI_TICK_MS, self._tick)
                except tk.TclError:
                    pass

    def _render(self, snap) -> None:
        if not xi.available():
            self._status.config(text="无 XInput", fg=theme.MUTE)
        elif snap is None or not snap.connected:
            self._status.config(text="无手柄", fg=theme.MUTE)
        else:
            slot = "—" if snap.slot is None else str(snap.slot)
            self._status.config(text=f"{slot} {snap.fps:.0f}Hz", fg=theme.OK)

        lx = ly = rx = ry = 0
        if snap is not None:
            lx, ly, rx, ry = snap.lx, snap.ly, snap.rx, snap.ry
        self._place_dot(self._lx_canvas, self._lx_dot, lx, ly)
        self._place_dot(self._rx_canvas, self._rx_dot, rx, ry)

        buttons = getattr(snap, "buttons", 0) if snap is not None else 0
        for name, (lbl, mask) in self._lamps.items():
            on = _lamp_on(buttons, name, mask)
            lbl.config(bg=theme.AMBER if on else theme.PANEL_2,
                       fg=theme.BG if on else theme.FAINT)

        self._lt_val = int(snap.lt8) if snap is not None else 0
        self._rt_val = int(snap.rt8) if snap is not None else 0
        self._paint_bar(self._lt_bar, self._lt_val)
        self._paint_bar(self._rt_bar, self._rt_val)

        tank = (snap.tank if snap is not None else None) or {}
        estop = int(tank.get("estop", 0) or 0)
        progress = float(snap.estop_progress or 0) if snap is not None else 0.0
        self._set_metric("左履带", f"{int(tank.get('track_l', 0) or 0):+d}", theme.INK)
        self._set_metric("右履带", f"{int(tank.get('track_r', 0) or 0):+d}", theme.INK)
        yaw = _YAW_TEXT.get(int(tank.get("yaw_rot", 0) or 0), "?")
        pitch = _PITCH_TEXT.get(int(tank.get("pitch_rot", 0) or 0), "?")
        self._set_metric("炮塔", f"{yaw}/{pitch}", theme.INK)
        rear = _REAR_TEXT.get(int(tank.get("rear_rot", 0) or 0), "?")
        self._set_metric("后臂", rear, theme.INK)
        if estop:
            self._set_metric("开火", "停", theme.MUTE)
            self._set_metric("急停", "锁", theme.BAD)
        else:
            fire = int(tank.get("fire", 0) or 0) == 1
            self._set_metric("开火", "开" if fire else "关", theme.BAD if fire else theme.MUTE)
            if 0 < progress < 1:
                self._set_metric("急停", f"{round(progress * 100)}%", theme.WARN)
            else:
                self._set_metric("急停", "0", theme.MUTE)

        send_text, send_level = self._send_view(snap)
        self._send_value.config(text=send_text, fg=_LEVEL_FG.get(send_level, theme.MUTE))
        cmd_text, cmd_level = format_server_line(self.server_snapshot())
        self._cmd_value.config(text=cmd_text, fg=_LEVEL_FG.get(cmd_level, theme.MUTE))

    @staticmethod
    def _send_view(snap) -> tuple[str, str]:
        if snap is None:
            return "—", "mute"
        if snap.transport == "udp":
            name = "UDP"
        elif snap.transport == "http":
            name = "HTTP"
        else:
            name = "—"
        if not snap.posted:
            return "停滞", "bad"
        if snap.last_error:
            return name, "warn"
        return name, "ok"

    def destroy(self) -> None:
        self._closed = True
        self.stop()
        super().destroy()
