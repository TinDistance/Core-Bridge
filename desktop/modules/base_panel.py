import tkinter as tk
from tkinter import ttk


class BasePanel(ttk.LabelFrame):
    """Shared frame for all desktop modules; one module per panel."""

    def __init__(self, master, title: str, **kwargs) -> None:
        super().__init__(master, text=title, **kwargs)
        self.build()

    def build(self) -> None:
        pass
