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

import os
import sys
import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

# 允许 `python desktop/modules/stream_panel.py` 直接跑自检（此时 sys.path[0]
# 是本文件所在目录，仓库根不在路径里，`desktop.*` 导不进来）。
if __package__ in (None, ""):  # pragma: no cover - 仅自检入口
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

from desktop import theme
from desktop.modules.base_panel import BasePanel
from desktop.streaming.h264_viewer import H264Viewer, available as h264_available
from desktop.streaming.viewer import Viewer

NO_STREAM_TEXT = "没有设备在推流"
NO_STREAM_HINT = "检查 K230 是否已上电，确认推流地址指向本机 UDP 8002"
SIGNAL_LOSS_GRACE_S = 5.0

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
        self._jpeg: Viewer | None = None
        self._h264_last = 0.0
        self._last_frame_ts = 0.0
        self._last_status = ""
        self._paused = False
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
        head = tk.Frame(self, bg=theme.PANEL)
        head.pack(fill=tk.X, padx=theme.PAD, pady=(theme.PAD, 6))
        tk.Label(head, text="VIDEO · 图传", bg=theme.PANEL, fg=theme.FAINT,
                 font=theme.FONT_EYEBROW).pack(side=tk.LEFT)
        self._pill_var = tk.StringVar(value="无信号")
        self._pill = tk.Label(head, textvariable=self._pill_var, bg="#232E42",
                               fg=theme.MUTE, font=theme.FONT_EYEBROW,
                               padx=8, pady=2)
        self._pill.pack(side=tk.RIGHT)
        self._meta_var = tk.StringVar(value="")
        tk.Label(head, textvariable=self._meta_var, bg=theme.PANEL,
                 fg=theme.FAINT, font=theme.FONT_MONO_SM).pack(side=tk.RIGHT, padx=(0, 8))

        bar = tk.Frame(self, bg=theme.PANEL)
        bar.pack(fill=tk.X, padx=theme.PAD, pady=(0, 6))
        tk.Label(bar, text="Server", bg=theme.PANEL, fg=theme.MUTE,
                 font=theme.FONT_SMALL).pack(side=tk.LEFT)
        self._server_var = tk.StringVar(value=self._default_server)
        ttk.Entry(bar, textvariable=self._server_var, width=26).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        self._toggle_btn = ttk.Button(bar, text="暂停", style="Accent.TButton",
                                      command=self._toggle)
        self._toggle_btn.pack(side=tk.LEFT)
        try:
            self._server_var.trace_add("write", lambda *_: self._on_server_changed())
        except Exception:
            pass

        self._canvas = tk.Canvas(self, bg="#0B0F16", highlightthickness=1,
                                 highlightbackground=theme.LINE)
        self._canvas.pack(fill=tk.BOTH, expand=True, padx=theme.PAD, pady=0)
        self._canvas.bind("<Configure>", self._on_configure)

        foot = tk.Frame(self, bg=theme.PANEL)
        foot.pack(fill=tk.X, padx=theme.PAD, pady=(6, theme.PAD))
        self._foot_var = tk.StringVar(value="等待首帧…")
        tk.Label(foot, textvariable=self._foot_var, bg=theme.PANEL,
                 fg=theme.MUTE, font=theme.FONT_MONO_SM).pack(side=tk.LEFT)
        self._size_var = tk.StringVar(value="")
        tk.Label(foot, textvariable=self._size_var, bg=theme.PANEL,
                 fg=theme.FAINT, font=theme.FONT_MONO_SM).pack(side=tk.RIGHT)

        # 接线延迟探针：provider 模式（单入口），render_age 不再恒 -1
        if self._monitor is not None:
            try:
                self._monitor.set_stream_stats_provider(
                    lambda: self._h264.stats() if self._h264 is not None else {})
            except Exception:
                pass
        self._poll_after = self.after(POLL_INTERVAL_MS, self._poll)

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
        try:
            self._stop_jpeg()
        except Exception:
            pass
        super().destroy()

    def _on_server_changed(self) -> None:
        """地址编辑后重建 viewer（防抖：由下次 _poll 实际执行）。"""
        # 标记强制重建：停掉旧对象，下轮 _ensure 会用新地址创建
        try:
            if self._h264 is not None:
                self._h264.stop()
                self._h264 = None
            self._stop_jpeg()
            self._h264_last = 0.0
            # 新 viewer 的解码尺寸可能不同，重新学一次比例（D4）
            self._src_size = DEFAULT_SRC_SIZE
            self._src_known = False
            self._render_size = (0, 0)
            self._can_scale = True
        except Exception:
            pass

    def _toggle(self) -> None:
        self._paused = not self._paused
        if self._paused:
            if self._h264 is not None:
                self._h264.stop()
                self._h264 = None
            self._stop_jpeg()
            self._streaming = False
            self._toggle_btn.config(text="继续看")
            self._set_pill("已暂停", theme.MUTE, "#232E42")
            self._render_size = (0, 0)
            self._src_known = False
            self._redraw()
        else:
            self._h264_last = time.monotonic()
            self._toggle_btn.config(text="暂停")

    def _ensure_h264(self) -> None:
        """H264 裸 RTP 是主链路，常驻。PyAV 缺失才退回 JPEG。"""
        if self._h264 is not None or not h264_available():
            return
        self._h264 = H264Viewer(self._server_var.get())
        # D4：能力探测。没有 set_render_size 的 viewer（例如旧版本）会退回
        # UI 线程 resize 路径。
        self._can_scale = hasattr(self._h264, "set_render_size")
        self._render_size = (0, 0)
        self._h264.start()
        # 预热窗口：刚创建尚未完成 IDR 握手，避免下一轮误拉 JPEG 双耗
        self._h264_last = time.monotonic()

    def _ensure_jpeg(self) -> None:
        """JPEG 链路仅在本机无 PyAV（无法解码 H264）时兜底拉起。"""
        if self._jpeg is None:
            self._jpeg = Viewer(self._server_var.get())
            self._jpeg.start()

    def _stop_jpeg(self) -> None:
        if self._jpeg is not None:
            try:
                self._jpeg.stop()
            except Exception:
                pass
            self._jpeg = None

    def _set_pill(self, text: str, fg: str, bg: str) -> None:
        self._pill_var.set(text)
        self._pill.config(fg=fg, bg=bg)

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            now = time.monotonic()
            if self._paused:
                return
            self._ensure_h264()
            # 无 PyAV：直接 JPEG（原 elif 逻辑导致黑屏）
            if not h264_available():
                self._ensure_jpeg()

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
                self._streaming = True
                t0 = time.monotonic()
                self._draw_frame(h264_img)
                self._draw_ms = (time.monotonic() - t0) * 1000.0
                if self._monitor is not None:
                    try:
                        self._monitor.report_draw(self._draw_ms)
                    except Exception:
                        pass
                self._set_pill("● LIVE", theme.OK, "#14352B")
                if self._last_status and self._last_status not in ("streaming",):
                    self._foot_var.set(f"{self._last_status} · {self._size_var.get()}")
            elif self._last_frame_ts and (now - self._last_frame_ts) < SIGNAL_LOSS_GRACE_S:
                self._streaming = True
                gap = now - self._last_frame_ts
                self._set_pill(f"重连中 {gap:.1f}s", theme.WARN, "#3A2E14")
                if self._last_status:
                    self._foot_var.set(f"{self._last_status} · 重连中 {gap:.1f}s")
            else:
                self._streaming = False
                self._photo = None
                self._size_var.set("")
                self._set_pill("无信号", theme.MUTE, "#232E42")
                self._redraw()
        finally:
            if not self._closed:
                try:
                    self._poll_after = self.after(POLL_INTERVAL_MS, self._poll)
                except Exception:
                    pass

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
            cw // 2, ch // 2 - 12, text=NO_STREAM_TEXT,
            fill=theme.INK, font=theme.cjk_font(14),
        )
        self._canvas.create_text(
            cw // 2, ch // 2 + 14, text=NO_STREAM_HINT,
            fill=theme.MUTE, font=theme.cjk_font(9),
        )


# ==================================================================== 自检
# 纯逻辑检查，**不实例化 Tk**（headless 环境没有显示器，Tk() 会直接抛错）。
# 这里能验的是 D4 里唯一有分支逻辑的部分：等比目标尺寸的计算与去抖判据。
# 「<Configure> -> set_render_size -> 下一帧以新尺寸到达」这条链路本身需要
# 显示器 + 真实 viewer，属于未经运行验证的部分。
#
#   python desktop/modules/stream_panel.py

def _test_fit_render_size_even_and_contained():
    """目标尺寸必须是偶数，且不能超出 canvas。"""
    for cw, ch in ((1280, 720), (1279, 719), (801, 601), (400, 400), (100, 900)):
        w, h = fit_render_size(1280, 720, cw, ch)
        assert w % 2 == 0 and h % 2 == 0, (cw, ch, w, h)
        assert w >= 2 and h >= 2, (cw, ch, w, h)
        assert w <= cw and h <= ch, (cw, ch, w, h)


def _test_fit_render_size_keeps_aspect():
    """等比：结果比例必须贴着源比例。

    容差是 ``2 * src_w``：``fit_render_size`` 两个方向都**向下**取偶数，
    高度方向最多丢 2 px，交叉乘积的误差就等于 ``2 * src_w``。这比 < 0.5% 的
    视觉差异，肉眼不可见。
    """
    for sw, sh in ((1280, 720), (1920, 1080), (640, 480), (720, 720)):
        for cw, ch in ((1280, 720), (900, 700), (500, 500), (1919, 1079)):
            w, h = fit_render_size(sw, sh, cw, ch)
            err = abs(w * sh - h * sw)
            assert err <= 2 * sw, (sw, sh, cw, ch, w, h, err)


def _test_fit_render_size_invalid_input():
    """任一输入 ≤ 0 -> (0, 0)，与 set_render_size(0,0) 的「不缩放」对齐。"""
    assert fit_render_size(1280, 720, 0, 0) == (0, 0)
    assert fit_render_size(0, 720, 800, 600) == (0, 0)
    assert fit_render_size(1280, 720, -1, 600) == (0, 0)
    # 极小 canvas 也不能返回 0（否则会误触发 viewer's「不缩放」分支）
    assert fit_render_size(1280, 720, 2, 2) == (2, 2)
    assert fit_render_size(1280, 720, 3, 3) == (2, 2)


def _test_ratio_changed_tolerance():
    """比例修正必须有死区：偶数取整误差不算变化，真变才算。"""
    # 偶数向下取整造成的 ~0.1% 偏差 -> 不修正
    assert not ratio_changed(1278, 718, 1280, 720)
    assert not ratio_changed(1280, 720, 1280, 720)
    # 1% 以内不修正（1280x710 vs 1280x720 = 1.4%）
    assert not ratio_changed(1280, 710, 1280, 720, tol=0.02)
    # 2.8%（1280x700）超过 2% 死区 -> 修正
    assert ratio_changed(1280, 700, 1280, 720, tol=0.02)
    # 明显变化（4:3 vs 16:9）-> 修正
    assert ratio_changed(640, 480, 1280, 720)
    # 同比例换分辨率（720p -> 1080p）不算变化
    assert not ratio_changed(1920, 1080, 1280, 720)
    assert not ratio_changed(0, 0, 1280, 720)


def _test_poll_interval_is_8ms():
    """D5：轮询周期必须是 8 ms（且只有一处常量，两处调度共用）。"""
    assert POLL_INTERVAL_MS == 8


def main():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("_test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    import sys
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
