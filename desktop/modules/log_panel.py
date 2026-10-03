"""Server 日志：只回答“刚才发生了什么”，默认自动跟随，报错行标红。"""
from __future__ import annotations

import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk

import httpx

from desktop import theme
from desktop.logs.log_client import LogClient
from desktop.modules.base_panel import BasePanel


_TIME_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?\s*")
_NUM = re.compile(r"\d+")

_POLLING_PATHS = (
    "/video/status",
    "/video/latest.jpg",
    "/video/timing",
    "/video/mjpeg",
    "/ping",
    "/test",
    "/command",
)

_SUMMARY_INTERVAL = 2.0


def _normalize_key(line: str) -> str:
    """去掉时间戳、折叠纯数字长串，得到“重复分组”键；保留 fps/帧号等关键数值差异。"""
    s = _TIME_PREFIX.sub("", line).strip()
    # 只折叠时间戳/长数字（≥4位）与内存地址，避免不同帧号/fps 被误折叠
    s = re.sub(r"\b\d{4,}\b", "#", s)
    s = re.sub(r"0x[0-9a-fA-F]+", "0x#", s)
    return s


def _is_polling(line: str) -> bool:
    if "uvicorn.access" not in line and "/video/" not in line and "/ping" not in line \
            and "/test" not in line and "/command" not in line:
        return False
    return any(p in line for p in _POLLING_PATHS)


def _classify_tag(line: str) -> str | None:
    low = line.lower()
    if "error" in low or "exception" in low or "failed" in low or "err:" in low:
        return "err"
    if "warn" in low:
        return "warn"
    if re.search(r"\bok\b", low) or "done" in low or "connected" in low:
        return "ok"
    return None


class LogPanel(BasePanel):
    def __init__(self, master, default_server: str = "ws://127.0.0.1:8000", **kwargs) -> None:
        self._default_server = default_server
        self._client: LogClient | None = None
        self._queue: queue.Queue = queue.Queue()
        self._follow = True
        self._dedup = True
        self._hide_polling = True
        self._closed = False
        self._pending_key: str | None = None
        self._pending_hidden: int = 0
        self._pending_since: float = 0.0
        self._dedup_hidden: int = 0
        self._polling_hidden: int = 0
        super().__init__(master, title="LOGS · 日志", **kwargs)

    def build(self) -> None:
        head = tk.Frame(self, bg=theme.PANEL)
        head.pack(fill=tk.X, padx=theme.PAD, pady=(theme.PAD, 6))
        tk.Label(head, text="LOGS · 日志", bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.FONT_EYEBROW).pack(side=tk.LEFT)
        self._toggle_btn = ttk.Button(head, text="连接", command=self._toggle)
        self._toggle_btn.pack(side=tk.RIGHT)
        ttk.Button(head, text="清空远端", command=self._clear_history).pack(side=tk.RIGHT, padx=(0, 6))
        self._follow_var = tk.StringVar(value="跟随开")
        ttk.Button(head, textvariable=self._follow_var,
                   command=self._flip_follow).pack(side=tk.RIGHT, padx=(0, 6))

        opts = tk.Frame(self, bg=theme.PANEL)
        opts.pack(fill=tk.X, padx=theme.PAD, pady=(0, 6))
        self._dedup_var = tk.BooleanVar(value=self._dedup)
        ttk.Checkbutton(opts, text="折叠重复", variable=self._dedup_var,
                        command=self._flip_dedup).pack(side=tk.LEFT)
        self._hide_poll_var = tk.BooleanVar(value=self._hide_polling)
        ttk.Checkbutton(opts, text="隐藏轮询", variable=self._hide_poll_var,
                        command=self._flip_hide_poll).pack(side=tk.LEFT, padx=(8, 0))
        self._fold_var = tk.StringVar(value="")
        tk.Label(opts, textvariable=self._fold_var, bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.FONT_SMALL).pack(side=tk.RIGHT)

        self._server_var = tk.StringVar(value=self._default_server)
        addr = tk.Frame(self, bg=theme.PANEL)
        addr.pack(fill=tk.X, padx=theme.PAD, pady=(0, 6))
        tk.Label(addr, text="Server", bg=theme.PANEL, fg=theme.MUTE,
                 font=theme.FONT_SMALL).pack(side=tk.LEFT)
        ttk.Entry(addr, textvariable=self._server_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        frame = tk.Frame(self, bg=theme.PANEL)
        frame.pack(fill=tk.BOTH, expand=True, padx=theme.PAD, pady=(0, theme.PAD))
        self._text = tk.Text(frame, wrap=tk.WORD, state=tk.DISABLED,
                             bg="#0B0F16", fg="#C7D2E2", insertbackground=theme.INK,
                             relief=tk.FLAT, highlightthickness=1,
                             highlightbackground=theme.LINE,
                             font=theme.cjk_font(9))
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._text.yview)
        self._text.configure(yscrollcommand=scrollbar.set)
        self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._text.tag_config("err", foreground=theme.BAD)
        self._text.tag_config("warn", foreground=theme.WARN)
        self._text.tag_config("ok", foreground=theme.OK)
        self._text.tag_config("fold", foreground=theme.FAINT)

        self.after(200, self._poll)
        # 默认自动连接（与 Monitor/Pusher 一致），失败仅打行日志不断裂
        try:
            self.after(500, self._auto_connect)
        except Exception:
            pass

    def _auto_connect(self) -> None:
        if self._client is None:
            try:
                self._toggle()
            except Exception:
                pass

    def _clear_history(self) -> None:
        """显式清服务端历史（按钮触发，不再初始化自动清）。"""
        url = self._server_var.get().rstrip("/")
        threading.Thread(target=self._clear_worker, args=(url,), daemon=True).start()

    def _clear_worker(self, url: str) -> None:
        try:
            httpx.delete(f"{url}/logs", timeout=2)
        except Exception:
            pass

    def _toggle(self) -> None:
        if self._client is None:
            self._queue = queue.Queue()
            self._reset_text()
            url = self._server_var.get().rstrip("/")
            if url.startswith("http://"):
                url = "ws://" + url[len("http://"):]
            elif url.startswith("https://"):
                url = "wss://" + url[len("https://"):]
            self._client = LogClient(url + "/ws/logs", self._queue)
            self._client.start()
            self._toggle_btn.config(text="断开")
        else:
            self._client.stop()
            self._client = None
            self._toggle_btn.config(text="连接")

    def _flip_follow(self) -> None:
        self._follow = not self._follow
        self._follow_var.set("跟随开" if self._follow else "跟随关")

    def _flip_dedup(self) -> None:
        self._dedup = bool(self._dedup_var.get())
        self._pending_key = None
        self._pending_hidden = 0

    def _flip_hide_poll(self) -> None:
        self._hide_polling = bool(self._hide_poll_var.get())

    def _refresh_fold_label(self) -> None:
        parts = []
        if self._dedup_hidden > 0:
            parts.append(f"重复 {self._dedup_hidden}")
        if self._polling_hidden > 0:
            parts.append(f"轮询 {self._polling_hidden}")
        if parts:
            self._fold_var.set("已省略 " + " · ".join(parts))
        else:
            self._fold_var.set("")

    def _reset_text(self) -> None:
        self._text.config(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.config(state=tk.DISABLED)
        self._pending_key = None
        self._pending_hidden = 0
        self._pending_since = 0.0
        self._dedup_hidden = 0
        self._polling_hidden = 0
        self._refresh_fold_label()

    def _append_line(self, line: str, tag: str | None) -> None:
        if tag:
            self._text.insert(tk.END, line + "\n", tag)
        else:
            self._text.insert(tk.END, line + "\n")

    def _trim(self) -> None:
        try:
            end_idx = self._text.index(tk.END)
            rows = int(end_idx.split(".")[0])
        except Exception:
            return
        if rows > 2000:
            try:
                self._text.delete("1.0", "500.0")
            except tk.TclError:
                pass

    def _flush_pending(self) -> None:
        """结算当前重复组：藏起来的条数落一行灰色小结。调用时 Text 须可写。"""
        if self._dedup and self._pending_hidden > 0:
            self._text.insert(
                tk.END,
                f"  ↳ 同类重复 {self._pending_hidden} 条已自动省略\n",
                "fold",
            )
            self._trim()
        self._pending_key = None
        self._pending_hidden = 0

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            self._text.config(state=tk.NORMAL)
        except tk.TclError:
            return
        try:
            while True:
                try:
                    line = self._queue.get_nowait()
                except queue.Empty:
                    break
                if self._hide_polling and _is_polling(line):
                    self._polling_hidden += 1
                    continue
                tag = _classify_tag(line)
                key = _normalize_key(line) if self._dedup else None
                if self._dedup and key is not None and key == self._pending_key:
                    self._pending_hidden += 1
                    self._dedup_hidden += 1
                    if time.monotonic() - self._pending_since >= _SUMMARY_INTERVAL:
                        self._flush_pending()
                    continue
                self._flush_pending()
                self._append_line(line, tag)
                self._trim()
                self._pending_key = key
                self._pending_hidden = 0
                self._pending_since = time.monotonic()
            if (self._dedup and self._pending_hidden > 0
                    and time.monotonic() - self._pending_since >= _SUMMARY_INTERVAL):
                self._flush_pending()
            self._refresh_fold_label()
        finally:
            try:
                if self._follow:
                    self._text.see(tk.END)
                self._text.config(state=tk.DISABLED)
            except tk.TclError:
                pass
            if not self._closed:
                try:
                    self.after(200, self._poll)
                except tk.TclError:
                    pass

    def destroy(self) -> None:  # type: ignore[override]
        self._closed = True
        try:
            if self._client is not None:
                self._client.stop()
                self._client = None
        except Exception:
            pass
        super().destroy()

    def stop(self) -> None:
        try:
            if self._client is not None:
                self._client.stop()
        except Exception:
            pass
