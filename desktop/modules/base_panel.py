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
