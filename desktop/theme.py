"""TinDistance pit-wall 主题 token：整个桌面 UI 的唯一颜色/字体来源。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

BG = "#12110E"
PANEL = "#1C1A16"
PANEL_2 = "#26231C"
LINE = "#3A3428"
INK = "#F4EFE6"
MUTE = "#B3A89A"
FAINT = "#7A7166"
AMBER = "#E2A23A"
OK = "#7FBF9A"
BAD = "#D4654F"
WARN = "#E2A23A"

FONT_TITLE = ("Segoe UI Semibold", 11)
FONT_BODY = ("Segoe UI", 9)
FONT_SMALL = ("Segoe UI", 9)
FONT_EYEBROW = ("Segoe UI Semibold", 9)
FONT_MONO = ("Consolas", 9)
FONT_MONO_BIG = ("Consolas", 26, "bold")
FONT_MONO_SM = ("Consolas", 9)
_CJK_CANDIDATES = (
    "微软雅黑", "Microsoft YaHei", "Microsoft YaHei UI",
    "微軟正黑體", "Microsoft JhengHei UI",
    "Noto Sans CJK SC", "Noto Sans CJK TC", "SimHei",
)
_cjk_family: str | None = None


def cjk_family() -> str:
    """本机可用的中文字体名；找不到则回退 Tk 默认字体（不断裂）。"""
    global _cjk_family
    if _cjk_family is None:
        try:
            import tkinter.font as tkfont
            avail = set(tkfont.families())
        except Exception:
            avail = set()
        _cjk_family = next(
            (n for n in _CJK_CANDIDATES if n in avail), "TkDefaultFont",
        )
    return _cjk_family


def cjk_font(size: int = 9) -> tuple:
    """中英混排控件的 font 参数（须在 Tk 根窗口创建后调用）。"""
    return (cjk_family(), size)

PAD = 10


def enable_dpi_awareness() -> None:
    """Windows 高分屏必调：声明 DPI 感知，否则系统会位图拉伸整个窗口，"""
    try:
        import ctypes
    except Exception:
        return
    try:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return
        try:
            windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except Exception:
            pass
        try:
            windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        try:
            windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


def apply_theme(root: tk.Tk | tk.Widget) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=BG, foreground=INK, font=FONT_BODY)
    style.configure("TFrame", background=BG)

    style.configure("TLabel", background=BG, foreground=INK, font=FONT_BODY)

    style.configure(
        "TButton",
        background=PANEL_2,
        foreground=INK,
        bordercolor=LINE,
        lightcolor=PANEL_2,
        darkcolor=PANEL_2,
        font=FONT_BODY,
        padding=(10, 5),
    )
    style.map(
        "TButton",
        background=[("active", "#4A4034"), ("disabled", PANEL)],
        foreground=[("disabled", FAINT)],
    )
    style.configure(
        "Accent.TButton",
        background=AMBER,
        foreground="#1A1206",
        bordercolor=AMBER,
        lightcolor=AMBER,
        darkcolor=AMBER,
        font=("Segoe UI Semibold", 9),
    )
    style.map(
        "Accent.TButton",
        background=[("active", "#F0B456"), ("disabled", PANEL_2)],
        foreground=[("disabled", FAINT)],
    )

    style.configure(
        "TEntry",
        fieldbackground=PANEL_2,
        background=PANEL_2,
        foreground=INK,
        bordercolor=LINE,
        lightcolor=LINE,
        darkcolor=LINE,
        insertcolor=INK,
    )
    style.configure("TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BG)
