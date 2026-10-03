"""延迟测量（桌面端探针 + 链路两端状态配合）。"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import httpx

WARN_MS = 500.0
HISTORY = 120

CLIENT_REPORT_MIN_S = 5.0

STREAM_METRIC_KEYS = (
    "render_age_ms",
    "queue_ms",
    "backlog_packets",
    "backlog_bytes",
    "drops",
    "loop_us_avg",
    "loop_us_max",
)

STREAM_PRIMARY_KEY = "render_age_ms"


def _num(v: object, lo: float | None = None) -> float:
    """把任意来源的字段安全转成 float，失败/缺失一律 -1（优雅降级）。"""
    try:
        if v is None:
            return -1.0
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1.0
    if x != x:
        return -1.0
    if lo is not None and x < lo:
        return -1.0
    return x


@dataclass
class LatencySample:
    t: float

    render_age_ms: float = -1.0

    staleness_ms: float = -1.0

    rtt_ms: float = -1.0

    fetch_ms: float = -1.0

    queue_ms: float = -1.0
    backlog_packets: float = -1.0
    backlog_bytes: float = -1.0
    drops: float = -1.0
    loop_us_avg: float = -1.0
    loop_us_max: float = -1.0

    fps: float = 0.0
    jitter_ms: float = 0.0
    frame_id: int = -1
    live: bool = False
    source: str = "udp:8001"

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
        self._fetch_ms: float | None = None
        self.last_error: str = ""
        self._stream_stats: dict = {}
        self._stream_provider: Callable[[], dict] | None = None
        self._stream_lock = threading.Lock()
        self._last_client_report = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="latency-probe")
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)

    def report_fetch(self, fetch_ms: float) -> None:
        """StreamPanel 的 Viewer 每次拉到帧后调用（本机取帧耗时）。"""
        with self._lock:
            self._fetch_ms = fetch_ms

    def report_draw(self, draw_ms: float) -> None:
        """绘制耗时（原 report_fetch 的真实语义）；暂与 fetch 共用字段，避免混入 local_ms 需另算。"""
        with self._lock:
            self._fetch_ms = draw_ms

    def report_stream_stats(self, stats: dict | None) -> None:
        """喂 H264Viewer.stats()。字段缺失/None 一律存 None，后续按 -1 显示。"""
        with self._stream_lock:
            if stats is None:
                return
            self._stream_stats = dict(stats)

    def set_stream_stats_provider(self, fn: Callable[[], dict] | None) -> None:
        """注册一个返回 viewer.stats() 的 callable，采样时主动拉。"""
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
                pass
        return cached

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
            self._append(LatencySample(t=time.time(), source="无信号"))
            return
        t0 = time.monotonic()
        try:
            resp = client.get(f"{base}/video/status")
            data = resp.json()
        except Exception as e:
            self.last_error = str(e)
            self._append(LatencySample(t=time.time()))
            return
        src = "udp:8001"
        if not data.get("live"):
            src = None
            try:
                t1 = time.monotonic()
                resp = client.get(f"{base}/video/rtp_status")
                rtp = resp.json()
                if rtp.get("live"):
                    data = rtp
                    src = "rtp:8002"
            except Exception:
                pass
            if src is None:
                src = "无信号"
        rtt = (time.monotonic() - t0) * 1000.0
        self.last_error = ""
        live = bool(data.get("live", False))
        ss = self._read_stream_stats()
        with self._lock:
            fetch_ms = self._fetch_ms

        sample = LatencySample(
            t=time.time(),
            render_age_ms=_num(ss.get("render_age_ms"), lo=0.0),
            staleness_ms=_num(data.get("staleness_ms"), lo=0.0),
            rtt_ms=round(rtt, 1),
            fetch_ms=round(fetch_ms, 1)
            if fetch_ms is not None and fetch_ms >= 0 else -1.0,
            queue_ms=_num(ss.get("queue_ms"), lo=0.0),
            backlog_packets=_num(ss.get("backlog_packets"), lo=0.0),
            backlog_bytes=_num(ss.get("backlog_bytes"), lo=0.0),
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
        """限频把本机指标快照推到 server，好让 /video/timing 能透出真实滞后。"""
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
            pass

    def _append(self, sample: LatencySample) -> None:
        with self._lock:
            self._samples.append(sample)

    def samples(self) -> list[LatencySample]:
        with self._lock:
            return list(self._samples)

    def current(self) -> LatencySample | None:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def stats(self) -> dict:
        samples = self.samples()
        cur = samples[-1] if samples else None
        vals = [s.render_age_ms for s in samples if s.render_age_ms >= 0]
        queue_vals = [s.queue_ms for s in samples if s.queue_ms >= 0]
        stale_vals = [s.staleness_ms for s in samples if s.staleness_ms >= 0]
        has_render = bool(vals)
        return {
            "current_ms": cur.render_age_ms if cur else -1,
            "render_age_ms": cur.render_age_ms if cur else -1,
            "latency_source": "render_age" if has_render else "unavailable",
            "latency_available": has_render,

            "local_ms": cur.local_ms if cur else -1,
            "rtt_ms": cur.rtt_ms if cur else -1,
            "fetch_ms": cur.fetch_ms if cur else -1,

            "staleness_ms": cur.staleness_ms if cur else -1,
            "staleness_max_ms": max(stale_vals) if stale_vals else -1,

            "queue_ms": cur.queue_ms if cur else -1,
            "queue_max_ms": max(queue_vals) if queue_vals else -1,
            "backlog_packets": cur.backlog_packets if cur else -1,
            "backlog_bytes": cur.backlog_bytes if cur else -1,
            "drops": cur.drops if cur else -1,
            "loop_us_avg": cur.loop_us_avg if cur else -1,
            "loop_us_max": cur.loop_us_max if cur else -1,

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