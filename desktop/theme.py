"""TinDistance pit-wall 主题 token：整个桌面 UI 的唯一颜色/字体来源。

设计立场（只为这台赛车控制台选的，不是通用模板）：
- 背景是赛道沥青蓝，而不是纯黑或米白纸面：长时间盯图传不刺眼，
  又和维修区夜间灯光的氛围一致。
- 主信号色是维修区信号灯琥珀 #FFB020，只用在延迟数字 + 曲线 + 关键操作上，
  其他地方全部压暗，让操作手的眼睛永远先落在延迟上。
- 辅助用遥测青 #4CC9FF，只画链接/次要曲线，不抢主信号。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

BG = "#0E131A"  # asphalt，应用底
PANEL = "#161E29"  # 卡片底
PANEL_2 = "#1C2534"  # 悬浮/输入底
LINE = "#263144"  # 分隔线
INK = "#EAF0F6"  # 主文字
MUTE = "#8A96AC"  # 次要文字
FAINT = "#5B6679"  # 极弱文字/网格
AMBER = "#FFB020"  # 主信号：延迟/录制/关键按钮
CYAN = "#4CC9FF"  # 辅助：链接/RTT
OK = "#34D399"
BAD = "#FF5D5D"
WARN = "#FFB020"

FONT_DISPLAY = ("Segoe UI Semibold", 32)  # 右上大延迟数字，tabular 感靠 Consolas 数字行
FONT_TITLE = ("Segoe UI Semibold", 11)
FONT_BODY = ("Segoe UI", 9)
FONT_SMALL = ("Segoe UI", 8)
FONT_EYEBROW = ("Segoe UI Semibold", 8)
FONT_MONO = ("Consolas", 9)
FONT_MONO_BIG = ("Consolas", 26, "bold")
FONT_MONO_SM = ("Consolas", 8)

PAD = 10
RADIUS_NOTE = "卡片统一用 1px LINE 描边 + 无圆角系统主题，靠间距分层，不用阴影。"


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
