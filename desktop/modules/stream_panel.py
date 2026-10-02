from desktop.modules.base_panel import BasePanel


class StreamPanel(BasePanel):
    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, title="屏幕推流", **kwargs)

    def build(self) -> None:
        ttk.Label(self, text="（待实现：推流屏幕）").pack(expand=True)
