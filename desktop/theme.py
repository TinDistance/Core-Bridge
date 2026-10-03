"""TinDistance pit-wall 主题 token：整个桌面 UI 的唯一颜色/字体来源。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

BG = "#0E131A"
PANEL = "#161E29"
PANEL_2 = "#1C2534"
LINE = "#263144"
INK = "#EAF0F6"
MUTE = "#8A96AC"
FAINT = "#7E8CA3"
AMBER = "#FFB020"
CYAN = "#4CC9FF"
OK = "#34D399"
BAD = "#FF5D5D"
WARN = "#FFB020"

FONT_DISPLAY = ("Segoe UI Semibold", 32)
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
RADIUS_NOTE = "卡片统一用 1px LINE 描边 + 无圆角系统主题，靠间距分层，不用阴影。"


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
    style.configure("Card.TFrame", background=PANEL)
    style.configure("Header.TFrame", background=PANEL)
    style.configure("Sidebar.TFrame", background=BG)

    style.configure("TLabel", background=BG, foreground=INK, font=FONT_BODY)
    style.configure("Card.TLabel", background=PANEL, foreground=INK)
    style.configure("Mute.TLabel", background=BG, foreground=MUTE, font=FONT_SMALL)
    style.configure("CardMute.TLabel", background=PANEL, foreground=MUTE, font=FONT_SMALL)
    style.configure(
        "Eyebrow.TLabel",
        background=PANEL,
        foreground=FAINT,
        font=FONT_EYEBROW,
    )
    style.configure(
        "HeaderEyebrow.TLabel",
        background=PANEL,
        foreground=MUTE,
        font=FONT_EYEBROW,
    )
    style.configure("Title.TLabel", background=BG, foreground=INK, font=FONT_TITLE)

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
        background=[("active", "#243044"), ("disabled", PANEL)],
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
        background=[("active", "#FFC14D"), ("disabled", PANEL_2)],
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
    style.configure("Card.TScrollbar", background=PANEL, troughcolor=PANEL)
