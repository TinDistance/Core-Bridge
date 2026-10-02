from desktop.modules.base_panel import BasePanel


class PlaceholderPanel(BasePanel):
    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, title="预留", **kwargs)

    def build(self) -> None:
        ttk.Label(self, text="（预留板块）").pack(expand=True)
