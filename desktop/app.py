import tkinter as tk
from tkinter import ttk

from desktop.modules.log_panel import LogPanel
from desktop.modules.placeholder_panel import PlaceholderPanel
from desktop.modules.stream_panel import StreamPanel


class DesktopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("TinDistance Desktop")
        self.geometry("1100x650")
        self.minsize(800, 450)

        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)
        container.columnconfigure(2, weight=1)
        container.rowconfigure(0, weight=1)

        self.stream_panel = StreamPanel(container)
        self.stream_panel.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        self.log_panel = LogPanel(container)
        self.log_panel.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)

        self.placeholder_panel = PlaceholderPanel(container)
        self.placeholder_panel.grid(row=0, column=2, sticky="nsew", padx=4, pady=4)


def main() -> None:
    app = DesktopApp()
    app.mainloop()


if __name__ == "__main__":
    main()
