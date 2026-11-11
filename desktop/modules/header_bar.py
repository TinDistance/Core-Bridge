"""顶栏：连接、档位、滞后、RTT、FPS、丢包、急停。不读探针，不做 HTTP。"""
from __future__ import annotations

import tkinter as tk

from desktop import theme

_LEVELS = {
    "ok": theme.OK,
    "warn": theme.WARN,
    "bad": theme.BAD,
    "mute": theme.MUTE,
}
_ESTOP_BG = "#A33B2E"


def format_tier(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "—"
    if width >= 1600 and height >= 900:
        return "T3 1080p"
    if width == 1280 and height == 720:
        return "720p"
    return f"{width}×{height}"


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


class HeaderBar(tk.Frame):
    def __init__(self, master, on_release_estop) -> None:
        super().__init__(master, bg=theme.PANEL, highlightthickness=1,
                         highlightbackground=theme.LINE)
        self._on_release_estop = on_release_estop
        self._values: dict[str, tk.StringVar] = {}
        self._labels: dict[str, tk.Label] = {}

        title = tk.Label(self, text="TinDistance", bg=theme.PANEL, fg=theme.INK,
                         font=theme.FONT_TITLE)
        title.pack(side=tk.LEFT, padx=(12, 16), pady=8)

        for key, caption in (
            ("connection", "连接"),
            ("tier", "档位"),
            ("latency", "滞后"),
            ("rtt", "RTT"),
            ("fps", "FPS"),
            ("loss", "丢包"),
        ):
            self._add_readout(key, caption)

        self._estop = tk.Button(
            self, text="急停", command=self._release,
            bg=theme.PANEL, fg=theme.MUTE, activebackground=_ESTOP_BG,
            activeforeground="#FFFFFF", relief=tk.FLAT, state=tk.DISABLED,
            font=theme.cjk_font(10), padx=10, pady=2,
        )
        self._estop.pack(side=tk.RIGHT, padx=(0, 12), pady=8)

    def _add_readout(self, key: str, caption: str) -> None:
        box = tk.Frame(self, bg=theme.PANEL)
        box.pack(side=tk.LEFT, padx=(0, 14), pady=6)
        tk.Label(box, text=caption, bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.FONT_EYEBROW).pack(anchor="w")
        var = tk.StringVar(value="—")
        label = tk.Label(box, textvariable=var, bg=theme.PANEL, fg=theme.MUTE,
                         font=theme.FONT_MONO)
        label.pack(anchor="w")
        self._values[key] = var
        self._labels[key] = label

    def _paint(self, key: str, text: str, level: str) -> None:
        self._values[key].set(text)
        self._labels[key].config(fg=_LEVELS.get(level, theme.MUTE))

    def set_connection(self, text: str, level: str) -> None:
        self._paint("connection", text, level)

    def set_tier(self, text: str) -> None:
        self._paint("tier", text, "mute")

    def set_latency(self, text: str, level: str) -> None:
        self._paint("latency", text, level)

    def set_rtt(self, text: str) -> None:
        self._paint("rtt", text, "mute")

    def set_fps(self, text: str) -> None:
        self._paint("fps", text, "mute")

    def set_loss(self, text: str, level: str) -> None:
        self._paint("loss", text, level)

    def set_estop(self, latched: bool, progress: float) -> None:
        if latched:
            self._estop.config(
                text="急停中 · 解除", bg=_ESTOP_BG, fg="#FFFFFF",
                state=tk.NORMAL,
            )
            return
        if 0 < progress < 1:
            self._estop.config(
                text=f"急停 {round(progress * 100)}%",
                bg=theme.PANEL, fg=theme.WARN, state=tk.DISABLED,
            )
            return
        self._estop.config(
            text="急停", bg=theme.PANEL, fg=theme.MUTE, state=tk.DISABLED,
        )

    def _release(self) -> None:
        if self._on_release_estop is not None:
            self._on_release_estop()
