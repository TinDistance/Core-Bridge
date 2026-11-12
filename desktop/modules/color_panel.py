"""白平衡色盘和基础调色。停在右侧栏最底下，不盖住图传。"""
from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path

from PIL import ImageTk

from desktop import theme
from desktop.streaming.color_grade import (
    ColorGrade,
    axis_from_unit,
    default_store_path,
    load_state,
    save_state,
    unit_from_axis,
    wb_field_image,
)

logger = logging.getLogger(__name__)

FIELD_W = 160
FIELD_H = 78
_SAVE_MS = 400

_SLIDERS = (
    ("brightness", "亮度"),
    ("contrast", "对比"),
    ("saturation", "饱和"),
)


class ColorGradePanel(tk.Frame):
    def __init__(self, master, on_change, store_path: Path | None = None,
                 persist: bool = True, on_layout=None) -> None:
        super().__init__(
            master, bg=theme.PANEL, highlightthickness=1,
            highlightbackground=theme.LINE, padx=8, pady=6,
        )
        self._on_change = on_change
        self._on_layout = on_layout
        self._path = default_store_path() if persist and store_path is None else store_path
        self._block = False
        self._closed = False
        self._save_after: str | None = None
        self._scales: dict[str, tk.Scale] = {}
        self._slider_vars: dict[str, tk.IntVar] = {}
        self._value_vars: dict[str, tk.StringVar] = {}
        loaded, open_panel = (ColorGrade(), True)
        if self._path is not None:
            loaded, open_panel = load_state(self._path)
        self._grade = ColorGrade()
        self._open = True

        head = tk.Frame(self, bg=theme.PANEL)
        head.pack(fill=tk.X)
        tk.Label(
            head, text="调色", bg=theme.PANEL, fg=theme.INK,
            font=theme.cjk_font(11),
        ).pack(side=tk.LEFT)
        self._fold = tk.Button(
            head, text="收起", command=self._toggle,
            relief=tk.FLAT, bd=0, padx=4, pady=0,
            bg=theme.PANEL, fg=theme.MUTE,
            activebackground=theme.PANEL_2, activeforeground=theme.INK,
            font=theme.cjk_font(9), cursor="hand2",
        )
        self._fold.pack(side=tk.RIGHT)
        tk.Button(
            head, text="重置", command=self._reset_all,
            relief=tk.FLAT, bd=0, padx=4, pady=0,
            bg=theme.PANEL, fg=theme.MUTE,
            activebackground=theme.PANEL_2, activeforeground=theme.INK,
            font=theme.cjk_font(9), cursor="hand2",
        ).pack(side=tk.RIGHT)

        self._body = tk.Frame(self, bg=theme.PANEL)
        self._build_pad(self._body)
        self._build_sliders(self._body)

        self._show_grade(loaded, notify=True, save=False)
        if not open_panel:
            self._open = False
            self._apply_open()
        else:
            self._body.pack(fill=tk.X, pady=(4, 0))
        self._notify_layout()

    def grade(self) -> ColorGrade:
        return self._grade

    def destroy(self) -> None:  # type: ignore[override]
        self._closed = True
        if self._save_after is not None:
            try:
                self.after_cancel(self._save_after)
            except tk.TclError:
                pass
            self._save_after = None
        self._write()
        super().destroy()

    def _build_pad(self, parent: tk.Frame) -> None:
        pad = tk.Frame(parent, bg=theme.PANEL)
        pad.pack()
        tk.Label(
            pad, text="冷", bg=theme.PANEL, fg=theme.FAINT, font=theme.cjk_font(8),
        ).grid(row=0, column=0, padx=(0, 4))
        self._pad = tk.Canvas(
            pad, width=FIELD_W, height=FIELD_H, bg="#14120E",
            highlightthickness=0, cursor="crosshair", bd=0,
        )
        self._pad.grid(row=0, column=1)
        tk.Label(
            pad, text="暖", bg=theme.PANEL, fg=theme.FAINT, font=theme.cjk_font(8),
        ).grid(row=0, column=2, padx=(4, 0))

        field = wb_field_image(FIELD_W, FIELD_H)
        self._field_photo = ImageTk.PhotoImage(field, master=self._pad)
        self._pad.create_image(0, 0, image=self._field_photo, anchor="nw")
        self._pad.create_text(
            FIELD_W / 2, 9, text="品红", fill=theme.INK, font=theme.cjk_font(8),
        )
        self._pad.create_text(
            FIELD_W / 2, FIELD_H - 9, text="绿", fill=theme.INK, font=theme.cjk_font(8),
        )
        cx = (FIELD_W - 1) / 2
        cy = (FIELD_H - 1) / 2
        self._pad.create_line(cx - 5, cy, cx + 5, cy, fill="#1A1206")
        self._pad.create_line(cx, cy - 5, cx, cy + 5, fill="#1A1206")
        self._cursor_halo = self._pad.create_oval(
            0, 0, 0, 0, outline="#1A1206", width=3,
        )
        self._cursor_ring = self._pad.create_oval(
            0, 0, 0, 0, outline=theme.INK, width=1,
        )
        self._pad.bind("<Button-1>", self._on_pad)
        self._pad.bind("<B1-Motion>", self._on_pad)
        self._pad.bind("<Double-Button-1>", self._reset_wb)

        readout = tk.Frame(parent, bg=theme.PANEL)
        readout.pack(pady=(4, 0))
        tk.Label(
            readout, text="色温", bg=theme.PANEL, fg=theme.MUTE, font=theme.cjk_font(9),
        ).pack(side=tk.LEFT)
        self._temp_var = tk.StringVar(value="+0")
        tk.Label(
            readout, textvariable=self._temp_var, bg=theme.PANEL, fg=theme.INK,
            font=theme.FONT_MONO, width=4, anchor="e",
        ).pack(side=tk.LEFT)
        tk.Label(
            readout, text="色调", bg=theme.PANEL, fg=theme.MUTE, font=theme.cjk_font(9),
        ).pack(side=tk.LEFT, padx=(10, 0))
        self._tint_var = tk.StringVar(value="+0")
        tk.Label(
            readout, textvariable=self._tint_var, bg=theme.PANEL, fg=theme.INK,
            font=theme.FONT_MONO, width=4, anchor="e",
        ).pack(side=tk.LEFT)

        rule = tk.Frame(parent, bg=theme.LINE, height=1)
        rule.pack(fill=tk.X, pady=(4, 4))

    def _build_sliders(self, parent: tk.Frame) -> None:
        for name, label in _SLIDERS:
            row = tk.Frame(parent, bg=theme.PANEL)
            row.pack(fill=tk.X, pady=1)
            tk.Label(
                row, text=label, bg=theme.PANEL, fg=theme.MUTE,
                font=theme.cjk_font(9), width=4, anchor="w",
            ).pack(side=tk.LEFT)
            slider = tk.IntVar(value=0)
            scale = tk.Scale(
                row, from_=-100, to=100, orient=tk.HORIZONTAL,
                resolution=1, showvalue=False, length=128,
                bg=theme.PANEL, fg=theme.INK, troughcolor="#12110E",
                activebackground=theme.AMBER, highlightthickness=0,
                bd=0, sliderlength=12, width=10, relief=tk.FLAT,
                variable=slider,
            )
            slider.trace_add(
                "write", lambda *_args, key=name: self._on_slider(key),
            )
            scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
            var = tk.StringVar(value="+0")
            tk.Label(
                row, textvariable=var, bg=theme.PANEL, fg=theme.INK,
                font=theme.FONT_MONO, width=4, anchor="e",
            ).pack(side=tk.LEFT)
            self._scales[name] = scale
            self._slider_vars[name] = slider
            self._value_vars[name] = var

    def _toggle(self) -> None:
        self._open = not self._open
        self._apply_open()
        self._schedule_save()

    def _apply_open(self) -> None:
        if self._open:
            if not self._body.winfo_ismapped():
                self._body.pack(fill=tk.X, pady=(4, 0))
            self._fold.configure(text="收起")
        else:
            self._body.pack_forget()
            self._fold.configure(text="展开")
        self._notify_layout()

    def _notify_layout(self) -> None:
        if self._on_layout is not None and not self._closed:
            self._on_layout()

    def _show_grade(self, grade: ColorGrade, notify: bool, save: bool) -> None:
        self._grade = grade.clamped()
        self._block = True
        try:
            for name, scale in self._scales.items():
                scale.set(getattr(self._grade, name))
            self._refresh_labels()
            self._place_cursor()
        finally:
            self._block = False
        if notify:
            self._on_change(self._grade)
        if save:
            self._schedule_save()

    def _refresh_labels(self) -> None:
        self._temp_var.set(f"{self._grade.temperature:+d}")
        self._tint_var.set(f"{self._grade.tint:+d}")
        for name, var in self._value_vars.items():
            var.set(f"{getattr(self._grade, name):+d}")

    def _place_cursor(self) -> None:
        x = unit_from_axis(self._grade.temperature) * (FIELD_W - 1)
        y = (1.0 - unit_from_axis(self._grade.tint)) * (FIELD_H - 1)
        self._pad.coords(self._cursor_halo, x - 6, y - 6, x + 6, y + 6)
        self._pad.coords(self._cursor_ring, x - 4, y - 4, x + 4, y + 4)

    def _on_pad(self, event: tk.Event) -> None:
        if self._block or self._closed:
            return
        temperature = axis_from_unit(event.x / (FIELD_W - 1))
        tint = axis_from_unit(1.0 - event.y / (FIELD_H - 1))
        if (temperature == self._grade.temperature
                and tint == self._grade.tint):
            return
        self._grade = ColorGrade(
            temperature, tint, self._grade.brightness,
            self._grade.contrast, self._grade.saturation,
        )
        self._refresh_labels()
        self._place_cursor()
        self._on_change(self._grade)
        self._schedule_save()

    def _reset_wb(self, _event: tk.Event | None = None) -> None:
        if self._grade.temperature == 0 and self._grade.tint == 0:
            return
        self._show_grade(ColorGrade(
            0, 0, self._grade.brightness, self._grade.contrast,
            self._grade.saturation,
        ), notify=True, save=True)

    def _on_slider(self, name: str) -> None:
        if self._block or self._closed:
            return
        try:
            value = int(round(float(self._slider_vars[name].get())))
        except (TypeError, ValueError, tk.TclError):
            return
        if getattr(self._grade, name) == value:
            self._value_vars[name].set(f"{value:+d}")
            return
        self._grade = ColorGrade(
            self._grade.temperature,
            self._grade.tint,
            value if name == "brightness" else self._grade.brightness,
            value if name == "contrast" else self._grade.contrast,
            value if name == "saturation" else self._grade.saturation,
        )
        self._value_vars[name].set(f"{value:+d}")
        self._on_change(self._grade)
        self._schedule_save()

    def _reset_all(self) -> None:
        self._show_grade(ColorGrade(), notify=True, save=True)

    def _schedule_save(self) -> None:
        if self._path is None or self._closed:
            return
        if self._save_after is not None:
            try:
                self.after_cancel(self._save_after)
            except tk.TclError:
                pass
        try:
            self._save_after = self.after(_SAVE_MS, self._flush_save)
        except tk.TclError:
            self._save_after = None

    def _flush_save(self) -> None:
        self._save_after = None
        self._write()

    def _write(self) -> None:
        if self._path is None:
            return
        try:
            save_state(self._grade, self._open, self._path)
        except OSError:
            logger.exception("failed to save color grade")
