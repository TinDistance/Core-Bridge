"""全场卡片基座：深底 + 1px 分隔线 + 眉题，不用系统 LabelFrame。"""
from __future__ import annotations

import tkinter as tk

from desktop import theme


class BasePanel(tk.Frame):
    """一个模块一张卡；标题是信息（它是什么流），不是装饰。"""

    def __init__(self, master, title: str, **kwargs) -> None:
        super().__init__(master, bg=theme.PANEL, highlightthickness=1,
                         highlightbackground=theme.LINE, **kwargs)
        self._title = title
        self.build()

    def build(self) -> None:
        pass

    def section_head(self, parent: tk.Widget, title: str) -> tk.Frame:
        head = tk.Frame(parent, bg=theme.PANEL)
        head.pack(fill=tk.X, padx=theme.PAD, pady=(theme.PAD, 0))
        tk.Label(head, text=title, bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.FONT_EYEBROW).pack(side=tk.LEFT)
        return head
