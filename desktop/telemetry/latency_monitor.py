"""延迟测量（桌面端探针 + 链路两端状态配合）。

链路：K230 --UDP:8001/8002--> server --HTTP--> desktop（server 与 desktop 同一台电脑）。


⚠️ 为什么主数字是 render_age_ms，不是 staleness，也不是 rtt+fetch
------------------------------------------------------------------------
在 4227d80 之前，这块面板干过两件自欺的事，现在都堵死了：

1. ``staleness_ms``（server 侧 ``now - 最新到达帧的戳``）被当延迟显示。
   新帧一直在到，所以它永远接近 0：30fps 满流 + 人为 4000ms 积压下实测
   最大只有 33.3ms，与零延迟链路**无法区分**。它测的是"最新数据有多旧"，
   不是"我看到的画面有多旧"（画面有多旧 = render_age_ms）。

2. ``e2e_ms = rtt + fetch`` 被当端到端延迟显示。这两项都是 localhost 量级
   （实测约 8.5ms），跟 5m 现场的 4~10s 毫无关系。一个与现场无关的本机
   数字顶着"延迟"的标签，比没有数字更糟 —— 它让人以为链路是好的。

现在的口径：

  * **主延迟数字 = ``render_age_ms``**：当前**上屏**那个 AU 的年龄
    （AU 到达桌面 -> 解码渲染完成）。这才是操作手眼睛看到的真实滞后。
    缺失时显示 ``—`` 并说明原因，**绝不**回退成任何一个假的延迟值。
  * ``local_ms``（原 e2e_ms 语义）：本机 HTTP 往返 + 本机拉帧耗时，
    改名并明确标注"不含空中段"，只作链路健康度/抖动旁证。
  * ``staleness_ms``：到达节奏，保留但改标签"到达陈旧"，明确非延迟。
  * ``queue_ms`` / ``backlog_packets`` / ``backlog_bytes`` / ``drops`` /
    ``loop_us_avg`` / ``loop_us_max``：来自 H264Viewer 的积压证据。
    ``drops`` 是"画面卡顿来自积压而不是链路断"的直接证据。

Agent A 契约（desktop/streaming/h264_viewer.py 的 ``stats()``）：
    render_age_ms, queue_ms, backlog_packets, backlog_bytes, drops,
    loop_us_avg, loop_us_max
这些字段由调用方通过 :meth:`report_stream_stats` 或
:meth:`set_stream_stats_provider` 喂进来。**任一字段缺失都优雅降级为 -1**
（面板显示 ``—``），绝不 KeyError。

采样两层，LatencyMonitor 后台线程每 500ms 一次：
  1. server 侧 HTTP：live / fps / frame_id / staleness / jitter + RTP relay
     的 kbps、pps、rcvbuf 上界。
  2. 本机 viewer 侧：render_age_ms / queue_ms / backlog / drops（进程内直接
     读，不走网络，所以它不受 server 与桌面同机的影响 —— 这是它比
     rtt+fetch 诚实得多的根本原因）。

历史保留 120 点（约 60s），LatencyPanel 直接读它画曲线；p50/p95 由
stats() 给出，>500ms 判 warn。

协议 v2（K230 侧 capture_ts）接入时：render_age_ms 覆盖的是"桌面内部
延迟"，仍不含 K230 采集 -> 编码 -> WiFi 发射这一段。要覆盖那一段需要
包头带采集时刻戳，届时在 _sample_once 里并入 render_age_ms 之上。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import httpx

WARN_MS = 500.0
HISTORY = 120

# 把本机指标快照（render_age_ms 等）上报到 server /video/timing/client 的
# 最小间隔。上报是为了让**不在操作手那台机器上**也能读到真实滞后（现场排障
# 只需要 curl server）。5s 足够及时，又不至于每个 500ms 采样都多一次 HTTP。
CLIENT_REPORT_MIN_S = 5.0

# Agent A 契约字段 -> 面板语义。键是 viewer.stats() 的键，值是语义分组。
# 全部 optional：viewer 还没上报就当 -1。
STREAM_METRIC_KEYS = (
    "render_age_ms",
    "queue_ms",
    "backlog_packets",
    "backlog_bytes",
    "drops",
    "loop_us_avg",
    "loop_us_max",
)

# render_age_ms 是主延迟数字，其余是解释"为什么是这个数"的证据。
STREAM_PRIMARY_KEY = "render_age_ms"


def _num(v: object, lo: float | None = None) -> float:
    """把任意来源的字段安全转成 float，失败/缺失一律 -1（优雅降级）。

    viewer.stats() 由 Agent A 维护，字段可能缺失、可能是 None、可能是
    字符串。任何一种都不允许让整块延迟面板崩掉 —— 崩掉等于整个操作手
    看不到延迟，比显示错数字更严重。
    """
    try:
        if v is None:
            return -1.0
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1.0
    if x != x:  # NaN
        return -1.0
    if lo is not None and x < lo:
        return -1.0
    return x


@dataclass
class LatencySample:
    t: float  # wall clock

    # ---- 主延迟：真实滞后。缺失即 -1，面板显示 "—"，绝不用别的数顶替 ----
    # 当前上屏 AU 的年龄（AU 到达桌面 -> 渲染完成）。这是操作手看到的
    # 画面到底有多旧。30fps 满流 + 4000ms 积压时它读 ~4000，而
    # staleness_ms 读 ~33 —— 这个差值就是整个改动的意义。
    render_age_ms: float = -1.0

    # ---- 到达节奏：server 侧 "最新帧到 server 有多久"。不是延迟。 ----
    # 保留是因为它能证明"还在收帧"（停流/卡顿的旁证），但绝不能加进延迟。
    staleness_ms: float = -1.0

    # ---- 本机成本：HTTP 往返。跟现场 4~10s 延迟无关。 ----
    rtt_ms: float = -1.0

    # ---- 本机取帧耗时（JPEG 链路），毫秒。viewer 侧 render_age 缺失时
    #      这是本地唯一能诚实测到的耗时，同样不含空中段。 ----
    fetch_ms: float = -1.0

    # ---- 积压证据（viewer 侧，缺失即 -1）----
    queue_ms: float = -1.0        # 接收队列等待估算（积压字节 / 码率）
    backlog_packets: float = -1.0  # 重排缓冲里待解码的包数
    backlog_bytes: float = -1.0    # 重排缓冲里待解码的字节数
    drops: float = -1.0            # 丢弃包总数（积压导致画面卡顿的直接证据）
    loop_us_avg: float = -1.0      # 接收循环平均耗时（微秒）
    loop_us_max: float = -1.0      # 接收循环峰值耗时（微秒）

    fps: float = 0.0
    jitter_ms: float = 0.0
    frame_id: int = -1
    live: bool = False
    source: str = "udp:8001"  # 数据来自哪个链路（JPEG hub / RTP relay）

    @property
    def local_ms(self) -> float:
        """本机往返 + 本机取帧。**不是延迟**，只当链路健康度看。"""
        if self.rtt_ms < 0:
            return -1.0
        v = self.rtt_ms
        if self.fetch_ms >= 0:
            v += self.fetch_ms
        return v

    @property
    def latency_ms(self) -> float:
        """面板主数字：真实滞后。没有就是没有（-1），绝不回退。"""
        return self.render_age_ms


class LatencyMonitor:
    def __init__(self, get_base_url, interval: float = 0.5) -> None:
        self._get_base_url = get_base_url
        self.interval = interval
        self._samples: deque[LatencySample] = deque(maxlen=HISTORY)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fetch_ms: float | None = None  # StreamPanel 注入的取帧耗时
        self.last_error: str = ""
        # viewer 侧指标（Agent A 契约）。两种喂法：
        #   report_stream_stats(dict)              —— StreamPanel 每帧调一次
        #   set_stream_stats_provider(callable)    —— 采样时主动拉一次
        self._stream_stats: dict = {}
        self._stream_provider: Callable[[], dict] | None = None
        self._stream_lock = threading.Lock()
        self._last_client_report = 0.0

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="latency-probe")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def report_fetch(self, fetch_ms: float) -> None:
        """StreamPanel 的 Viewer 每次拉到帧后调用（本机取帧耗时）。"""
        self._fetch_ms = fetch_ms

    # ---------- viewer 侧指标（Agent A 契约） ----------
    def report_stream_stats(self, stats: dict | None) -> None:
        """喂 H264Viewer.stats()。字段缺失/None 一律存 None，后续按 -1 显示。

        StreamPanel 持有 viewer，所以在它出帧时调用本方法即可把
        render_age_ms 送进面板。目前 StreamPanel 不属于本改动的文件范围，
        接线见报告"未做的事"。
        """
        with self._stream_lock:
            if stats is None:
                return
            self._stream_stats = dict(stats)

    def set_stream_stats_provider(self, fn: Callable[[], dict] | None) -> None:
        """注册一个返回 viewer.stats() 的 callable，采样时主动拉。

        相比 report_stream_stats 更好的地方：viewer 停止/切链路后能立刻
        反映"没有数据"，而不是留着最后一帧的陈旧值继续显示。
        """
        with self._stream_lock:
            self._stream_provider = fn

    def _read_stream_stats(self) -> dict:
        with self._stream_lock:
            provider = self._stream_provider
            cached = dict(self._stream_stats)
        if provider is not None:
            try:
                got = provider()
                if isinstance(got, dict):
                    with self._stream_lock:
                        self._stream_stats = dict(got)
                        cached = dict(got)
            except Exception:
                # viewer 崩了/正在停止：保留上一份，下一个周期再试。
                # 这里绝不能让采样线程挂掉 —— 它一挂整块面板就永久停更。
                pass
        return cached

    # ---------- 采样 ----------
    def _loop(self) -> None:
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                while not self._stop.is_set():
                    self._sample_once(client)
                    self._stop.wait(self.interval)
        finally:
            self._thread = None

    def _sample_once(self, client: httpx.Client) -> None:
        try:
            base = (self._get_base_url() or "").rstrip("/")
        except Exception:
            base = ""
        if not base:
            return
        t0 = time.monotonic()
        try:
            resp = client.get(f"{base}/video/status")
            rtt = (time.monotonic() - t0) * 1000.0
            data = resp.json()
        except Exception as e:
            self.last_error = str(e)
            self._append(LatencySample(t=time.time()))
            return
        # H264 裸 RTP 模式下 JPEG hub 无流，改读 rtp_relay 状态（字段兼容）
        src = "udp:8001"
        if not data.get("live"):
            src = None
            try:
                resp = client.get(f"{base}/video/rtp_status")
                rtp = resp.json()
                if rtp.get("live"):
                    data = rtp
                    src = "rtp:8002"
            except Exception:
                pass
            if src is None:
                # 两条链路都无流：src 必须有值，否则 LatencySample(source=src)
                # 会 NameError，整个延迟面板停止更新。
                src = "无信号"
        self.last_error = ""
        live = bool(data.get("live", False))
        ss = self._read_stream_stats()

        sample = LatencySample(
            t=time.time(),
            # 真实滞后来自 viewer 侧。viewer 没上报就是没有 —— 不回退到
            # staleness 或 rtt+fetch，那两个都不是延迟。
            render_age_ms=_num(ss.get("render_age_ms"), lo=0.0),
            # 到达节奏，保留但只是旁证，绝不进延迟。
            staleness_ms=_num(data.get("staleness_ms"), lo=0.0),
            rtt_ms=round(rtt, 1),
            fetch_ms=round(self._fetch_ms, 1)
            if self._fetch_ms is not None and self._fetch_ms >= 0 else -1.0,
            queue_ms=_num(ss.get("queue_ms"), lo=0.0),
            backlog_packets=_num(ss.get("backlog_packets"), lo=0.0),
            backlog_bytes=_num(ss.get("backlog_bytes"), lo=0.0),
            # drops 是累计量，允许 0；负数无意义 -> -1
            drops=_num(ss.get("drops"), lo=0.0),
            loop_us_avg=_num(ss.get("loop_us_avg"), lo=0.0),
            loop_us_max=_num(ss.get("loop_us_max"), lo=0.0),
            fps=_num(data.get("fps"), lo=0.0),
            jitter_ms=_num(data.get("jitter_ms"), lo=0.0),
            frame_id=int(data.get("frame_id", -1) or -1),
            live=live,
            source=src or "无信号",
        )
        self._append(sample)
        self._maybe_report_client_timing(client, base, sample)

    def _maybe_report_client_timing(
        self, client: httpx.Client, base: str, s: LatencySample
    ) -> None:
        """限频把本机指标快照推到 server，好让 /video/timing 能透出真实滞后。

        只在真的有 render_age_ms 时才上报 —— 上报一堆 None 对排障没意义，
        还会在 viewer 没起来时白刷一份"未知"快照覆盖掉上一次的有效值。
        """
        if s.render_age_ms < 0:
            return
        now = time.monotonic()
        if now - self._last_client_report < CLIENT_REPORT_MIN_S:
            return
        self._last_client_report = now
        body = {
            "render_age_ms": s.render_age_ms,
            "queue_ms": s.queue_ms,
            "backlog_packets": s.backlog_packets,
            "backlog_bytes": s.backlog_bytes,
            "drops": s.drops,
            "loop_us_avg": s.loop_us_avg,
            "loop_us_max": s.loop_us_max,
        }
        body = {k: v for k, v in body.items() if v >= 0}
        try:
            client.post(f"{base}/video/timing/client", json=body)
        except Exception:
            # 上报是旁路，失败不影响采样线程 —— 它一挂整块面板就停更。
            pass

    def _append(self, sample: LatencySample) -> None:
        with self._lock:
            self._samples.append(sample)

    # ---------- 查询（UI 线程调用） ----------
    def samples(self) -> list[LatencySample]:
        with self._lock:
            return list(self._samples)

    def current(self) -> LatencySample | None:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def stats(self) -> dict:
        samples = self.samples()
        cur = samples[-1] if samples else None
        # p50/p95 只对真实延迟（render_age）统计。用 rtt+fetch 统计出来的
        # 分位数会被当"延迟 p95"，那还是在给一个本机数字贴延迟标签。
        vals = [s.render_age_ms for s in samples if s.render_age_ms >= 0]
        # 陈旧度与积压的历史峰值：解释"卡顿来自积压"的证据
        queue_vals = [s.queue_ms for s in samples if s.queue_ms >= 0]
        stale_vals = [s.staleness_ms for s in samples if s.staleness_ms >= 0]
        has_render = bool(vals)
        return {
            # ---- 主数字：真实滞后。缺失 = -1，面板显示 "—" ----
            "current_ms": cur.render_age_ms if cur else -1,
            "render_age_ms": cur.render_age_ms if cur else -1,
            # 面板需要区分"没数据"和"有数据但是 0ms"，故单列来源标记
            "latency_source": "render_age" if has_render else "unavailable",
            "latency_available": has_render,

            # ---- 本机成本：明确不是延迟 ----
            "local_ms": cur.local_ms if cur else -1,
            "rtt_ms": cur.rtt_ms if cur else -1,
            "fetch_ms": cur.fetch_ms if cur else -1,

            # ---- 到达节奏：非延迟 ----
            "staleness_ms": cur.staleness_ms if cur else -1,
            "staleness_max_ms": max(stale_vals) if stale_vals else -1,

            # ---- 积压证据 ----
            "queue_ms": cur.queue_ms if cur else -1,
            "queue_max_ms": max(queue_vals) if queue_vals else -1,
            "backlog_packets": cur.backlog_packets if cur else -1,
            "backlog_bytes": cur.backlog_bytes if cur else -1,
            "drops": cur.drops if cur else -1,
            "loop_us_avg": cur.loop_us_avg if cur else -1,
            "loop_us_max": cur.loop_us_max if cur else -1,

            # ---- 基础状态 ----
            "fps": cur.fps if cur else 0,
            "jitter_ms": cur.jitter_ms if cur else 0,
            "frame_id": cur.frame_id if cur else -1,
            "live": cur.live if cur else False,
            "source": cur.source if cur else None,
            "p50_ms": round(_percentile(vals, 0.5), 1) if vals else -1,
            "p95_ms": round(_percentile(vals, 0.95), 1) if vals else -1,
            "count": len(vals),
        }


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]