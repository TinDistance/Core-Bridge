"""Server 日志：只回答“刚才发生了什么”，默认自动跟随，报错行标红。

高频轮询（/video/status、/video/latest.jpg 等）与连续重复行默认批量
折叠省略，避免把面板刷屏；工具条可随时关掉折叠查看原文。
"""
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


# 时间戳前缀（server 格式 "%Y-%m-%d %H:%M:%S,ms"）每次都不同，归一化时去掉，
# 否则同一条 access 日志永远判不成“重复”。
_TIME_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?\s*")
# 数字（帧号/延迟ms/端口/包大小…）折成 #，让“同类不同参”也算同一组重复。
_NUM = re.compile(r"\d+")

# 桌面自己发出的高频轮询：LatencyMonitor 0.5s 一次 /video/status，
# Viewer 30fps 轮询 /video/latest.jpg，健康检查 /command 等。
_POLLING_PATHS = (
    "/video/status",
    "/video/latest.jpg",
    "/video/latency",
    "/video/mjpeg",
    "/ping",
    "/test",
    "/command",
)

# 同一组重复超过这么久还没断，插一条小结落数，避免计数永远不落地。
# （比如 30fps 的图传拉帧日志停不下来时，每 2s 只多一行小结。）
_SUMMARY_INTERVAL = 2.0


def _normalize_key(line: str) -> str:
    """去掉时间戳、把数字折叠，得到“重复分组”键。"""
    s = _TIME_PREFIX.sub("", line).strip()
    s = _NUM.sub("#", s)
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
    if "ok" in low or "done" in low or "connected" in low:
        return "ok"
    return None


class LogPanel(BasePanel):
    def __init__(self, master, default_server: str = "ws://127.0.0.1:8000", **kwargs) -> None:
        self._default_server = default_server
        self._client: LogClient | None = None
        self._queue: queue.Queue = queue.Queue()
        self._follow = True
        # 批量省略状态：连续重复只展示首条，其余计数+定时落小结；
        # 轮询类（30fps 拉帧、0.5s 探针…）默认整类隐藏
        self._dedup = True
        self._hide_polling = True
        self._pending_key: str | None = None
        self._pending_hidden: int = 0
        self._pending_since: float = 0.0
        self._collapsed_total: int = 0
        super().__init__(master, title="LOGS · 日志", **kwargs)

    def build(self) -> None:
        head = tk.Frame(self, bg=theme.PANEL)
        head.pack(fill=tk.X, padx=theme.PAD, pady=(theme.PAD, 6))
        tk.Label(head, text="LOGS · 日志", bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.FONT_EYEBROW).pack(side=tk.LEFT)
        self._toggle_btn = ttk.Button(head, text="连接", command=self._toggle)
        self._toggle_btn.pack(side=tk.RIGHT)
        self._follow_var = tk.StringVar(value="跟随开")
        ttk.Button(head, textvariable=self._follow_var,
                   command=self._flip_follow).pack(side=tk.RIGHT, padx=(0, 6))

        # 第二排：批量省略开关 + 已省略计数
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

        self.after(0, self._clear_remote_history)
        self.after(200, self._poll)

    # ---------- 连接 ----------
    def _clear_remote_history(self) -> None:
        url = self._default_server.rstrip("/")
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
        # 关掉折叠后丢弃未结算的组（计数已计入总数，不再补小结行）
        self._pending_key = None
        self._pending_hidden = 0

    def _flip_hide_poll(self) -> None:
        self._hide_polling = bool(self._hide_poll_var.get())

    def _refresh_fold_label(self) -> None:
        if self._collapsed_total > 0:
            self._fold_var.set(f"已省略 {self._collapsed_total} 条重复")
        else:
            self._fold_var.set("")

    def _reset_text(self) -> None:
        self._text.config(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.config(state=tk.DISABLED)
        self._pending_key = None
        self._pending_hidden = 0
        self._pending_since = 0.0
        self._collapsed_total = 0
        self._refresh_fold_label()

    # ---------- 输出（只追加不改写，避免 Tk 文本索引跨版本算错行） ----------
    def _append_line(self, line: str, tag: str | None) -> None:
        if tag:
            self._text.insert(tk.END, line + "\n", tag)
        else:
            self._text.insert(tk.END, line + "\n")

    def _trim(self) -> None:
        if float(self._text.index(tk.END).split(".")[0]) > 2000:
            self._text.delete("1.0", "500.0")

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
        self._text.config(state=tk.NORMAL)
        try:
            while True:
                try:
                    line = self._queue.get_nowait()
                except queue.Empty:
                    break
                # 轮询类（30fps 图传拉帧、0.5s 状态探针…）默认整类隐藏
                if self._hide_polling and _is_polling(line):
                    self._collapsed_total += 1
                    continue
                tag = _classify_tag(line)
                key = _normalize_key(line) if self._dedup else None
                if self._dedup and key is not None and key == self._pending_key:
                    # 连续重复：只计数不展示；超 2s 不断则先落一条小结
                    self._pending_hidden += 1
                    self._collapsed_total += 1
                    if time.monotonic() - self._pending_since >= _SUMMARY_INTERVAL:
                        self._flush_pending()
                    continue
                # 新的一组：先结算上一组，再展示首条
                self._flush_pending()
                self._append_line(line, tag)
                self._trim()
                self._pending_key = key
                self._pending_hidden = 0
                self._pending_since = time.monotonic()
            # 队列见底时，超时未断的组也落地，避免小结迟迟不出现
            if (self._dedup and self._pending_hidden > 0
                    and time.monotonic() - self._pending_since >= _SUMMARY_INTERVAL):
                self._flush_pending()
            self._refresh_fold_label()
        finally:
            if self._follow:
                self._text.see(tk.END)
            self._text.config(state=tk.DISABLED)
            self.after(200, self._poll)
