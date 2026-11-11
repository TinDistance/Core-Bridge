"""推流画面：全场最大的一块，操作手的眼睛 80% 时间在这里。

D4/D5（doc §2.3、§4.5）
----------------------
缩放与 yuv→rgb 转换**全部**交给解码线程（`H264Viewer.set_render_size` +
swscale `frame.reformat`），UI 线程每帧只剩 `ImageTk.PhotoImage` + `create_image`。

- 旧实现每帧 `img.copy()`（720p 0.94 ms / 1080p 2.27 ms）+
  `img.resize()`（1080p→1280×720 bilinear 8.26 ms），合计最多 ~10.5 ms/帧，
  全部花在 UI 线程上；同样的变换用 swscale `reformat` 只要 0.50–0.60 ms
  （附录 A.3）。现在 `_draw_frame` **不再 resize、不再 copy**。
- `viewer` 侧的 `reformat` 是无脑拉伸，不做等比适配，所以**等比目标尺寸由本文件
  计算**（`fit_render_size`），并且只用偶数宽高（奇数尺寸下 `reformat` 的行为
  未验证）。只在目标尺寸变化时才调 `set_render_size`，不是每帧调。
- 轮询周期 `after(33)` → `after(8)`（D5），配合 `desktop/app.py` 里的
  `timeBeginPeriod(1)`（Tk `after` 在 Windows 上同样受 15.6 ms 定时器粒度影响）。
"""
from __future__ import annotations

import logging
import os
import sys
import time
import tkinter as tk

from PIL import Image, ImageTk

# 允许 `python desktop/modules/stream_panel.py` 直接跑自检（此时 sys.path[0]
# 是本文件所在目录，仓库根不在路径里，`desktop.*` 导不进来）。
if __package__ in (None, ""):  # pragma: no cover - 仅自检入口
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

from desktop import theme
from desktop.modules.base_panel import BasePanel
from desktop.streaming.h264_viewer import H264Viewer, available as h264_available

NO_STREAM_TEXT = "无画面"
SIGNAL_LOSS_GRACE_S = 5.0
RECONNECT_AFTER_S = 0.5

# D5：33 → 8 ms。旧值在 Windows 上让帧在 Tk 事件队列里平均多等 16 ms
# （Tk `after` 与 `Event.wait` 一样按 ~15.6 ms 定时器粒度取整，附录 A.1）。
# 8 ms 配合进程入口的 timeBeginPeriod(1) 才能真正生效。
POLL_INTERVAL_MS = 8

# 首帧到达前不知道解码尺寸，用 16:9 作为占位比例（K230 只有 1280×720 一档，
# K10）。注意：占位比例**不会**在首帧前下发给 viewer（见 `_sync_render_size`
# 的 `_src_known` 闸门），首帧会以解码原始尺寸到达并纠正真实比例 —— 所以即使
# 将来推的是非 16:9 源也不会拉伸。
DEFAULT_SRC_SIZE = (1280, 720)

# 源比例修正死区。等比缩放会引入 < 0.2% 的取整误差（偶数向下取整），
# 1% 以下的偏差一律不修正，避免每帧微调目标尺寸形成抖动。
RATIO_TOLERANCE = 0.01

logger = logging.getLogger(__name__)


def ratio_changed(new_w: int, new_h: int,
                  ref_w: int, ref_h: int,
                  tol: float = RATIO_TOLERANCE) -> bool:
    """``(new_w, new_h)`` 与 ``(ref_w, ref_h)`` 的宽高比是否差异超过 ``tol``。

    纯逻辑，便于离线验证。这里用交叉乘积而不是浮点除法：缩放后的尺寸是
    「偶数向下取整」的结果，直接比 float 会把 0.1% 的取整误差当成变化。
    """
    if min(new_w, new_h, ref_w, ref_h) <= 0:
        return False
    return abs(new_w * ref_h - new_h * ref_w) > tol * new_w * ref_h


def fit_render_size(src_w: int, src_h: int,
                    canvas_w: int, canvas_h: int) -> tuple[int, int]:
    """按 canvas 尺寸做等比适配，返回**偶数**的 ``(w, h)``。

    - 等比：``scale = min(cw/src_w, ch/src_h)``，与旧 `_draw_frame` 里的算法一致。
    - 偶数：``H264Viewer.set_render_size` 最终会走 swscale ``reformat``，
      奇数尺寸的行为未验证，所以两个方向都向下取偶数。向下取整同时保证
      结果不会超出 canvas（canvas 本身可能是奇数）。
    - 任一输入 ≤ 0 时返回 ``(0, 0)``，与 ``set_render_size(0, 0)`` 的
      「不缩放」语义对齐（调用方应当跳过下发）。
    """
    if min(int(src_w), int(src_h), int(canvas_w), int(canvas_h)) <= 0:
        return (0, 0)
    scale = min(canvas_w / src_w, canvas_h / src_h)
    nw = max(2, int(src_w * scale))
    nh = max(2, int(src_h * scale))
    return (nw - (nw & 1), nh - (nh & 1))


class StreamPanel(BasePanel):
    def __init__(self, master, default_server: str = "http://127.0.0.1:8000",
                 monitor=None, **kwargs) -> None:
        self._default_server = default_server
        self._monitor = monitor
        self._h264: H264Viewer | None = None
        self._h264_last = 0.0
        self._last_frame_ts = 0.0
        self._last_status = ""
        self._closed = False
        self._photo: ImageTk.PhotoImage | None = None
        self._streaming = False
        self._frames = 0
        self._draw_ms = 0.0
        self._poll_after: str | None = None
        # ---- D4 状态 ------------------------------------------------------
        # 解码源尺寸（只用宽高比，绝对值不参与目标尺寸计算 —— 已缩放的帧
        # 仍然保持等比，所以从缩放后的帧也能反推出正确的比例）。
        self._src_size: tuple[int, int] = DEFAULT_SRC_SIZE
        self._src_known = False
        # 已下发给 viewer.set_render_size 的目标尺寸；(0, 0) = 未下发 =
        # 解码线程不做缩放，帧是解码原始尺寸。
        self._render_size: tuple[int, int] = (0, 0)
        # viewer 没有 set_render_size（老版本）时的兼容标记，此时退回 UI 线程
        # resize。正常路径下永远为 True。
        self._can_scale = True
        super().__init__(master, title="VIDEO · 图传", **kwargs)

    def build(self) -> None:
        self._canvas = tk.Canvas(self, bg="#14120E", highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._canvas.bind("<Configure>", self._on_configure)
        self._foot_var = tk.StringVar(value="")
        self._size_var = tk.StringVar(value="")
        if self._monitor is not None:
            try:
                self._monitor.set_stream_stats_provider(
                    lambda: self._h264.stats() if self._h264 is not None else {})
                self._monitor.set_base_url(self._default_server)
            except Exception:
                logger.exception("failed to wire telemetry provider")
        self._poll_after = self.after(POLL_INTERVAL_MS, self._poll)

    def source_size(self) -> tuple[int, int]:
        if self._src_known:
            return self._src_size
        return (0, 0)

    def signal_state(self) -> str:
        if self._last_frame_ts <= 0:
            return "wait"
        age = time.monotonic() - self._last_frame_ts
        if age >= SIGNAL_LOSS_GRACE_S:
            return "wait"
        if age >= RECONNECT_AFTER_S:
            return "reconnect"
        return "live"

    def destroy(self) -> None:  # type: ignore[override]
        self._closed = True
        try:
            if self._poll_after is not None:
                self.after_cancel(self._poll_after)
        except Exception:
            pass
        try:
            if self._h264 is not None:
                self._h264.stop()
        except Exception:
            pass
        super().destroy()

    def _ensure_h264(self) -> None:
        """K230 只推 H.264 RTP。没有 PyAV 时不另开图传。"""
        if self._h264 is not None or not h264_available():
            return
        self._h264 = H264Viewer(self._default_server)
        self._can_scale = hasattr(self._h264, "set_render_size")
        self._render_size = (0, 0)
        self._h264.start()
        self._h264_last = time.monotonic()

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            now = time.monotonic()
            self._ensure_h264()
            if not h264_available():
                self._last_status = "本机没有 PyAV，无法解码 H.264"

            h264_img = None
            h264_status = None
            if self._h264 is not None:
                for kind, payload in self._h264.events():
                    if kind == "frame":
                        h264_img = payload
                    elif kind == "status":
                        h264_status = payload
            if h264_status:
                self._last_status = str(h264_status)

            if h264_img is not None:
                self._last_frame_ts = now
                t0 = time.monotonic()
                self._draw_frame(h264_img)
                self._draw_ms = (time.monotonic() - t0) * 1000.0
                if self._monitor is not None:
                    try:
                        self._monitor.report_draw(self._draw_ms)
                    except Exception:
                        pass
            state = self.signal_state()
            if state == "reconnect":
                self._streaming = True
                if h264_img is None:
                    self._reposition()
                self._paint_reconnect(now)
            elif state == "wait":
                self._streaming = False
                self._photo = None
                self._size_var.set("")
                self._draw_placeholder()
            else:
                self._streaming = True
        finally:
            if not self._closed:
                try:
                    self._poll_after = self.after(POLL_INTERVAL_MS, self._poll)
                except Exception:
                    pass

    def _paint_reconnect(self, now: float) -> None:
        c = self._canvas_size()
        if c is None or self._last_frame_ts <= 0:
            return
        age = now - self._last_frame_ts
        self._canvas.delete("reconnect")
        self._canvas.create_text(
            c[0] // 2, 28, text=f"重连 {age:.1f}s",
            fill=theme.WARN, font=theme.cjk_font(12), tags="reconnect",
        )

    # ------------------------------------------------------------------ D4
    def _canvas_size(self) -> tuple[int, int] | None:
        try:
            cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        except tk.TclError:
            return None
        if cw < 2 or ch < 2:
            return None
        return cw, ch

    def _learn_src_ratio(self, w: int, h: int) -> None:
        """从到达的帧里学（并在源分辨率变化时修正）解码源的宽高比。

        首帧到达时 ``_render_size`` 还是 ``(0, 0)``，帧是解码原始尺寸，所以
        学到的就是真实比例。之后帧是**已缩放**的，但等比缩放保住了比例，
        所以仍然可以用它纠偏（换分辨率时不会拉伸）。偏差在
        ``RATIO_TOLERANCE``（1%）以内的一律忽略，避免偶数取整误差引起抖动。
        """
        if w <= 0 or h <= 0:
            return
        if self._src_known and not ratio_changed(w, h,
                                                 self._src_size[0], self._src_size[1]):
            return
        self._src_size = (w, h)
        self._src_known = True

    def _sync_render_size(self) -> tuple[int, int]:
        """按当前 canvas 尺寸算等比目标尺寸，**只在变化时**下发给 viewer。

        「只在变化时下发」同时充当 `<Configure>` 的去抖：拖动窗口时 Tk 以
        ~60 Hz 触发 Configure，但目标尺寸是偶数取整后的结果，1~2 px 的抖动
        多数轮次会算出同一个目标，不会重复调用 `set_render_size`。

        ``_src_known`` 闸门：首帧之前不下发。占位比例（16:9）只是兜底，
        拿它先下发会让首帧被按错误比例拉伸。
        """
        viewer = self._h264
        if viewer is None or not self._src_known or not self._can_scale:
            return self._render_size
        c = self._canvas_size()
        if c is None:
            return self._render_size
        want = fit_render_size(self._src_size[0], self._src_size[1], c[0], c[1])
        if want == (0, 0) or want == self._render_size:
            return self._render_size
        try:
            viewer.set_render_size(want[0], want[1])
        except Exception:
            # 老版本 viewer / 线程已死：退回 UI 线程 resize 路径
            self._can_scale = False
            return self._render_size
        self._render_size = want
        return want

    def _on_configure(self, _event=None) -> None:
        """``<Configure>``：更新目标尺寸 + 把已有画面重定位到新中心。

        **不**做任何像素变换。旧实现是 `_redraw()` 拿缓存的解码原图按新尺寸
        重缩放；D4 之后缩放在解码线程里，缓存/在屏的帧是**旧目标尺寸**，
        重新贴上去就是拉伸。所以这里只做两件与像素无关的事：

        1. 算新的等比目标尺寸，变化时才 ``set_render_size``；画面等下一帧
           以新尺寸自然到达（≤ 1 帧 ≈ 33 ms）；
        2. 把现有 ``PhotoImage`` 在新中心重新 ``create_image`` —— 像素尺寸
           不变，只是挪位置，不会变形，也不会白闪一帧。
        """
        self._sync_render_size()
        self._reposition()

    def _reposition(self) -> None:
        """把在屏画面挪到当前 canvas 中心（像素尺寸不变 → 不会变形）。"""
        if not self._streaming or self._photo is None:
            return
        c = self._canvas_size()
        if c is None:
            return
        self._canvas.delete("all")
        self._canvas.create_image(c[0] // 2, c[1] // 2, image=self._photo)

    def _redraw(self) -> None:
        """无信号 / 暂停时画占位符；有信号时只重定位（见 `_on_configure`）。

        旧实现会用 ``_pil_cache`` 里的解码原图按新 canvas 尺寸重缩放。D4 之后
        这条路已经多余且危险（缓存帧是旧尺寸 → 拉伸），所以 ``_pil_cache``
        被整个删掉：缩放归解码线程，本函数再没有任何像素变换的职责。
        """
        if self._streaming:
            self._reposition()
            return
        self._draw_placeholder()

    def _draw_frame(self, img: Image.Image) -> None:
        """贴一帧。D4：设过 render_size 时 ``img`` 已经是目标尺寸。

        这里**不再** ``resize``、**不再** ``copy()``：变换已在解码线程用 swscale
        完成（0.50–0.60 ms vs 旧路径合计最多 10.5 ms）。``copy()`` 在事件驱动
        下唯一的用途是给 ``<Configure>`` 重缩放当缓存，而那条路已被删除，
        所以每帧那次 0.94/2.27 ms 的整帧拷贝也一并消失。
        """
        c = self._canvas_size()
        if c is None:
            return
        cw, ch = c
        fw, fh = int(img.size[0]), int(img.size[1])
        self._learn_src_ratio(fw, fh)
        if self._can_scale:
            # 首帧刚学完比例：本帧还是解码原始尺寸，先下发目标尺寸，
            # 下一帧开始就是目标尺寸。
            self._sync_render_size()
        else:
            # 兼容路径：viewer 没有 set_render_size，只能在 UI 线程缩放。
            scale = min(cw / fw, ch / fh)
            nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
            if (nw, nh) != (fw, fh):
                img = img.resize((nw, nh), Image.BILINEAR)
                fw, fh = nw, nh
        self._photo = ImageTk.PhotoImage(img)
        self._canvas.delete("all")
        self._canvas.create_image(cw // 2, ch // 2, image=self._photo)
        self._frames += 1
        self._foot_var.set(f"#{self._frames} · {fw}x{fh}")
        self._size_var.set(f"{fw}x{fh}")

    def _draw_placeholder(self) -> None:
        if self._streaming:
            return
        c = self._canvas_size()
        if c is None:
            return
        cw, ch = c
        self._canvas.delete("all")
        self._canvas.create_text(
            cw // 2, ch // 2, text=NO_STREAM_TEXT,
            fill=theme.INK, font=theme.cjk_font(14),
        )
