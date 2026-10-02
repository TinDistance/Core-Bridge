import queue
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from desktop.modules.base_panel import BasePanel
from desktop.streaming.viewer import Viewer

NO_STREAM_TEXT = "没有设备在推流"


class StreamPanel(BasePanel):
    def __init__(self, master, default_server: str = "http://127.0.0.1:8000", **kwargs) -> None:
        self._default_server = default_server
        self._viewer: Viewer | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._streaming = False
        super().__init__(master, title="推流屏幕", **kwargs)

    def build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(top, text="Server:").pack(side=tk.LEFT)
        self._server_var = tk.StringVar(value=self._default_server)
        ttk.Entry(top, textvariable=self._server_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        self._toggle_btn = ttk.Button(top, text="停止观看", command=self._toggle)
        self._toggle_btn.pack(side=tk.LEFT)

        self._canvas = tk.Canvas(self, bg="#1e1e1e", highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self._canvas.bind("<Configure>", lambda _: self._draw_placeholder())

        self.after(500, self._ensure_viewer)
        self.after(100, self._poll)

    def _toggle(self) -> None:
        if self._viewer is None:
            self._ensure_viewer()
            self._toggle_btn.config(text="停止观看")
        else:
            self._viewer.stop()
            self._viewer = None
            self._streaming = False
            self._toggle_btn.config(text="开始观看")
            self._draw_placeholder()

    def _ensure_viewer(self) -> None:
        if self._viewer is None:
            self._viewer = Viewer(self._server_var.get())
            self._viewer.start()

    def _poll(self) -> None:
        if self._viewer is not None:
            for kind, payload in self._viewer.events():
                if kind == "frame":
                    self._streaming = True
                    self._draw_frame(payload)
                elif kind == "status":
                    if payload == "streaming":
                        self._streaming = True
                    elif payload == "no_stream":
                        self._streaming = False
                        self._draw_placeholder()
        self.after(100, self._poll)

    def _draw_frame(self, img: Image.Image) -> None:
        cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        if cw < 2 or ch < 2:
            return
        fw, fh = img.size
        scale = min(cw / fw, ch / fh)
        img = img.resize((max(1, int(fw * scale)), max(1, int(fh * scale))))
        self._photo = ImageTk.PhotoImage(img)
        self._canvas.delete("all")
        self._canvas.create_image(cw // 2, ch // 2, image=self._photo)

    def _draw_placeholder(self) -> None:
        if self._streaming:
            return
        cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        self._canvas.delete("all")
        if cw > 2 and ch > 2:
            self._canvas.create_text(
                cw // 2, ch // 2, text=NO_STREAM_TEXT, fill="#8a8a8a", font=("Segoe UI", 14)
            )
