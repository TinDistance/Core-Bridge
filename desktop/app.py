import tkinter as tk
from tkinter import ttk

from desktop.modules.log_panel import LogPanel
from desktop.modules.placeholder_panel import PlaceholderPanel
from desktop.modules.stream_panel import StreamPanel
from desktop.server_manager import ServerManager
from desktop.start_screen import StartScreen


class DesktopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("TinDistance Desktop")
        self.geometry("1100x650")
        self.minsize(800, 450)

        self._server: ServerManager | None = None
        self._start_screen: StartScreen | None = None

        self._show_start_screen()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _show_start_screen(self) -> None:
        self._start_screen = StartScreen(self, on_ready=self._on_server_ready)
        self._start_screen.pack(fill=tk.BOTH, expand=True)

    def _on_server_ready(self, server: ServerManager) -> None:
        self._server = server
        if self._start_screen is not None:
            self._start_screen.destroy()
            self._start_screen = None
        self._build_main()

    def _build_main(self) -> None:
        base_url = self._server.base_url

        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)
        container.columnconfigure(2, weight=1)
        container.rowconfigure(0, weight=1)

        self.stream_panel = StreamPanel(container, default_server=base_url)
        self.stream_panel.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        self.log_panel = LogPanel(container, default_server=base_url)
        self.log_panel.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)

        self.placeholder_panel = PlaceholderPanel(container)
        self.placeholder_panel.grid(row=0, column=2, sticky="nsew", padx=4, pady=4)

    def _on_close(self) -> None:
        if self._server is not None:
            self._server.stop()
        self.destroy()


def main() -> None:
    app = DesktopApp()
    app.mainloop()


if __name__ == "__main__":
    main()
