"""延迟检测方案（桌面端探针 + 服务端状态配合）。

链路：K230 --UDP:8001--> server --HTTP--> desktop（server 与 desktop 同一台电脑，
localhost 环节只有 1~2ms，所以右上曲线的主体是 K230 到 server 的空中延迟）。

三层测量，全部由本 Monitor 在后台线程里每 500ms 采样一次：
1. link_rtt_ms  本机 -> server 的 HTTP 往返（GET /video/status 计时）。
   链路断了它会先跳变，是最快的心跳。
2. frame_age_ms  server 报告的最新帧龄（now - latest_at）。
   包含 K230 采集 + UDP 重组 + server  hold + 桌面轮询间隔，是空中延迟的主体。
3. e2e_ms        对操作手真正有意义的数：frame_age_ms + link_rtt_ms。
   server 与桌面同机时约等于“镜头前动一下，到屏幕上看到要多久”。

历史保留 120 点（约 60s），LatencyPanel 直接读它画曲线；
p50/p95 由 stats() 给出，>500ms 判 warn，断流判 bad。

未来想更准：K230 在 UDP 头里加 8 字节 capture_ts（protocol v2），
server 回传 capture->server 分段耗时，本文件 report_fetch() 已预留 fetch_ms
注入位，届时 e2e 可拆成 空中段 / 本地段 两条曲线。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

import httpx

WARN_MS = 500.0
HISTORY = 120


@dataclass
class LatencySample:
    t: float  # wall clock
    rtt_ms: float  # 本机->server RTT
    age_ms: float  # server 帧龄（-1 表示无帧）
    e2e_ms: float  # age + rtt（无帧时 = -1）
    fps: float
    jitter_ms: float
    frame_id: int
    live: bool
    source: str = "udp:8001"  # 数据来自哪个链路（JPEG hub / RTP relay）


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


class LatencyMonitor:
    def __init__(self, get_base_url, interval: float = 0.5) -> None:
        self._get_base_url = get_base_url
        self.interval = interval
        self._samples: deque[LatencySample] = deque(maxlen=HISTORY)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fetch_ms: float | None = None  # Viewer 注入的最新帧拉取耗时（可选）
        self.last_error: str = ""

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
        """StreamPanel 的 Viewer 每次拉到帧后可调用，做 e2e 微调（可选）。"""
        self._fetch_ms = fetch_ms

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
            with self._lock:
                self._samples.append(
                    LatencySample(
                        t=time.time(),
                        rtt_ms=-1,
                        age_ms=-1,
                        e2e_ms=-1,
                        fps=0,
                        jitter_ms=0,
                        frame_id=-1,
                        live=False,
                    )
                )
            return
        # H264 裸 RTP 模式下 JPEG hub 无流，改读 rtp_relay 状态（字段兼容）
        if not data.get("live"):
            try:
                resp = client.get(f"{base}/video/rtp_status")
                rtp = resp.json()
                if rtp.get("live"):
                    data = rtp
                    src = "rtp:8002"
            except Exception:
                pass
        else:
            src = "udp:8001"
        self.last_error = ""
        age = float(data.get("age_ms", -1))
        live = bool(data.get("live", False))
        if not live or age < 0:
            e2e = -1.0
        else:
            e2e = age + max(0.0, rtt)
            if self._fetch_ms is not None:
                e2e += max(0.0, self._fetch_ms)
        with self._lock:
            self._samples.append(
                LatencySample(
                    t=time.time(),
                    rtt_ms=round(rtt, 1),
                    age_ms=age,
                    e2e_ms=round(e2e, 1) if e2e >= 0 else -1,
                    fps=float(data.get("fps", 0) or 0),
                    jitter_ms=float(data.get("jitter_ms", 0) or 0),
                    frame_id=int(data.get("frame_id", -1)),
                    live=live,
                    source=src,
                )
            )

    # ---------- 查询（UI 线程调用） ----------
    def samples(self) -> list[LatencySample]:
        with self._lock:
            return list(self._samples)

    def current(self) -> LatencySample | None:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def stats(self) -> dict:
        vals = [s.e2e_ms for s in self.samples() if s.e2e_ms >= 0]
        cur = self.current()
        return {
            "current_ms": cur.e2e_ms if cur else -1,
            "rtt_ms": cur.rtt_ms if cur else -1,
            "age_ms": cur.age_ms if cur else -1,
            "fps": cur.fps if cur else 0,
            "jitter_ms": cur.jitter_ms if cur else 0,
            "frame_id": cur.frame_id if cur else -1,
            "live": cur.live if cur else False,
            "source": cur.source if cur else None,
            "p50_ms": round(_percentile(vals, 0.5), 1) if vals else -1,
            "p95_ms": round(_percentile(vals, 0.95), 1) if vals else -1,
            "count": len(vals),
        }
