"""到达节奏检测（桌面端探针 + 服务端状态配合）。

链路：K230 --UDP:8001/8002--> server --HTTP--> desktop（server 与 desktop 同一台电脑）。

⚠️ 这里的 e2e_ms 目前**不是**端到端延迟，全链路没有任何采集时刻戳。
   server 上报的 staleness_ms 是"最新帧落到 server 有多久"（now - latest_at），
   30fps 满流时恒在 [0,33]ms：WiFi 发送队列压 4 秒、UDP 重传堆积，它一概看不见。
   旧实现把它加进 e2e，于是真实延迟 4000ms 的链路面板显示 ~50ms、状态"正常"，
   WARN_MS=500 这条告警线在链路有流时**永远不可能触发**（等于死代码）。
   现在 e2e 只累加两项真实成本：HTTP 往返 RTT + 本机拉帧耗时，都在 localhost
   量级（1~10ms）。真正的端到端要等 K230 包头带 capture_ts（protocol v2）。

两层采样，LatencyMonitor 后台线程每 500ms 一次：
1. rtt_ms        本机 -> server 的 HTTP 往返（GET /video/status 计时）。
                 链路断了它先跳变，是最快的心跳；也是当前 e2e 的主要成分。
2. staleness_ms  server 报告的到达陈旧度。**不是延迟**，只证明"还在收帧"，
                 链路有流时小于一个帧间隔，停流才飙升；面板单列一格标"陈旧度"，
                 不进 e2e。
3. e2e_ms        max(0, rtt) + max(0, fetch_ms)。不含空中段，见开头 ⚠️。

历史保留 120 点（约 60s），LatencyPanel 直接读它画曲线；
p50/p95 由 stats() 给出，>500ms 判 warn，断流判 bad。
500ms 阈值现在实际只对 RTT/拉帧有意义；空中段告警要等 capture_ts 落地。

协议 v2（capture_ts）接入时的落点：本文件 _sample_once() 里 e2e 的算法
与 HAS_CAPTURE_TS 标志，届时把空中段并入并给面板补第二条曲线。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

import httpx

WARN_MS = 500.0
HISTORY = 120

# 真实端到端延迟的数据源标记。False = K230 侧还没有采集时刻戳，e2e_ms 只能
# 由 RTT + 本机拉帧两项 localhost 成本构成，**不含 WiFi 空中段**。
# 后续任务在包头加 capture_ts（protocol v2）后置 True，并在 _sample_once 里
# 把采集到 server、server 到桌面两段并入 e2e。
HAS_CAPTURE_TS: bool = False


@dataclass
class LatencySample:
    t: float  # wall clock
    rtt_ms: float  # 本机->server RTT
    # server 侧到达陈旧度（now - 最新帧落 server 的时刻），-1 表示无帧。
    # 是"帧有多新"，**不是延迟**：30fps 满流恒在 [0,33]ms，WiFi 队列压 4s 也
    # 读不出差别。所以它只当停流/抖动的旁证，不进 e2e。
    staleness_ms: float
    # max(0, rtt) + max(0, fetch)；无帧时 -1。当前不含空中段（见模块头 ⚠️）
    e2e_ms: float
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
                        staleness_ms=-1,
                        e2e_ms=-1,
                        fps=0,
                        jitter_ms=0,
                        frame_id=-1,
                        live=False,
                    )
                )
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
                # 两条链路都无流：src 必须有值，否则下面 LatencySample(source=src)
                # 会 NameError，整个延迟面板停止更新。
                src = "无信号"
        self.last_error = ""
        staleness = float(data.get("staleness_ms", -1))
        live = bool(data.get("live", False))
        if not live or staleness < 0:
            e2e = -1.0
        else:
            # 只累加两项真实成本：HTTP 往返 + 本机拉帧耗时。
            # staleness 绝不能加进来 —— 它是 server 收包节奏，WiFi 空中段的
            # 积压它看不见，加进去等于把 4s 延迟粉饰成 ~50ms"正常"。
            # HAS_CAPTURE_TS 翻转之前，这里就是全部能诚实测到的量。
            e2e = max(0.0, rtt)
            if self._fetch_ms is not None:
                e2e += max(0.0, self._fetch_ms)
        with self._lock:
            self._samples.append(
                LatencySample(
                    t=time.time(),
                    rtt_ms=round(rtt, 1),
                    staleness_ms=staleness,
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
            # 与 current_ms 同值，给不想猜语义的地方一个明确名字
            "e2e_ms": cur.e2e_ms if cur else -1,
            "rtt_ms": cur.rtt_ms if cur else -1,
            # 陈旧度：到达节奏，不是延迟；面板单独一格，不进 e2e
            "staleness_ms": cur.staleness_ms if cur else -1,
            "fps": cur.fps if cur else 0,
            "jitter_ms": cur.jitter_ms if cur else 0,
            "frame_id": cur.frame_id if cur else -1,
            "live": cur.live if cur else False,
            "source": cur.source if cur else None,
            "p50_ms": round(_percentile(vals, 0.5), 1) if vals else -1,
            "p95_ms": round(_percentile(vals, 0.95), 1) if vals else -1,
            "count": len(vals),
        }
