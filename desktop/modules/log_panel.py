from desktop.modules.base_panel import BasePanel


class LogPanel(BasePanel):
    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, title="Server 日志", **kwargs)

    def build(self) -> None:
        ttk.Label(self, text="（待实现：Server 日志）").pack(expand=True)
