"""延迟显示口径验证：证明"旧口径测不出、新口径测得出"。

    python test/test_latency_display.py

背景
----
在 4227d80 之前，面板上的"延迟"是两个与现场无关的数：

  * ``staleness_ms`` = now - 最新**到达** server 的帧的时间戳。新帧一直在
    到，所以它永远接近 0。实测 30fps 满流 + 人为 4000ms 积压下最大只有
    33.3ms，与零延迟链路无法区分。它测的是"最新数据有多旧"。
  * ``e2e_ms`` = rtt + fetch，两项都是 localhost 量级（约 8.5ms）。

而操作手真正要问的是"我看到的画面有多旧"。答案在桌面进程里：当前**上屏**
那个 AU 从到达桌面到渲染完成过了多久 —— ``render_age_ms``。

本测试构造同一条链路上并排的两个读数：

    假 K230 --30fps--> RtpRelay(:18004, 真实实现) --> 桌面接收端
                                                              |
                                        桌面端人为压住 4000ms 才"渲染"

真实发生的：relay 收到的是**新鲜、稳定的 30fps**（所以它的
``staleness_ms`` 必须读 ≈33ms，与零延迟链路无法区分）；桌面接收端手里
压着 4000ms 的积压，所以 ``render_age_ms`` 必须读 ≈4000ms。

关键点：这 4000ms 的积压就是真实世界里 socket 接收缓冲 + 重排缓冲的排队
延迟 —— 包早就到了，只是没被读走。旧口径对它是完全瞎的。

``_ViewerStub`` 是 H264Viewer.stats() 的**契约替身**：Agent A 的
``stats()`` 尚未产出约定字段时，用它按约定键名产出指标；Agent A 落地后
真实 ``stats()`` 用同样的键名，本测试对延迟口径的断言对真实实现同样成立。
测试末尾会打印真实 ``H264Viewer.stats()`` 当前覆盖了契约里的哪些键。
"""
from __future__ import annotations

import asyncio
import socket
import struct
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import rtp_relay
from server.rtp_relay import RtpRelay
from server.routers import video as video_router
from desktop.telemetry.latency_monitor import STREAM_METRIC_KEYS, LatencyMonitor

# ---- 端口（本测试独占）----
RTP_PORT = 18004

FPS = 30
# 单包总长（12 字节 RTP 头 + payload）必须 <= 2048：relay._loop 用
# recvfrom(2048) 收包，超过会被内核截断，relay 就永远判不出 live。
# 真实 k230/rtp_push.py 同样是 1200 字节 payload。
PKTS_PER_AU = 2
PAYLOAD = 1000
assert 12 + PAYLOAD <= 2048
SSRC = 0x54494E44

# 注入的桌面端排队延迟。这就是要被"测出来"的那个数。
INJECT_HOLD_MS = 4000.0

# 判定阈值
STALENESS_MAX_MS = 60.0     # 满流下一个帧间隔(33ms) + 余量
RENDER_AGE_MIN_MS = 3500.0  # 注入 4000ms，读数下限
RENDER_AGE_MAX_MS = 4600.0  # 注入 4000ms，读数上限

PANEL_KEYS = (
    "current_ms", "render_age_ms", "latency_source", "latency_available",
    "local_ms", "rtt_ms", "staleness_ms", "queue_ms", "queue_max_ms",
    "backlog_packets", "backlog_bytes", "drops", "loop_us_avg", "loop_us_max",
    "fps", "jitter_ms", "frame_id", "live", "source", "p50_ms", "p95_ms",
    "count",
)


def rtp_pkt(seq: int, ts: int, marker: bool) -> bytes:
    head = struct.pack(">BBHII", 0x80, 0x60 | (0x80 if marker else 0),
                       seq & 0xFFFF, ts & 0xFFFFFFFF, SSRC)
    return head + bytes(PAYLOAD)


class _Source(threading.Thread):
    """假 K230：按 30fps 打 RTP（marker 位收尾）。

    不做编码 —— relay 与本测试的接收端只看 RTP 版本位、marker 位和负载长度。
    """

    def __init__(self, port: int) -> None:
        super().__init__(daemon=True, name="fake-k230")
        self._port = port
        self._stop = threading.Event()
        self.sent = 0

    def run(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        target = ("127.0.0.1", self._port)
        seq = 0
        n = 0
        try:
            while not self._stop.is_set():
                ts = n * (90000 // FPS)
                for i in range(PKTS_PER_AU):
                    last = i == PKTS_PER_AU - 1
                    try:
                        s.sendto(rtp_pkt(seq, ts, last), target)
                    except OSError:
                        pass
                    seq = (seq + 1) & 0xFFFF
                self.sent += 1
                n += 1
                self._stop.wait(1.0 / FPS)
        finally:
            s.close()

    def stop(self) -> None:
        self._stop.set()


class _ViewerStub(threading.Thread):
    """桌面接收端契约替身：真实收包、真实按 marker 组 AU，人为压住再渲染。

    ``render_age_ms`` = 渲染完成时刻 - 该 AU 首包到达时刻，也就是"我看到的
    画面有多旧"。

    注意它**不是**"解码花了多久"：后者在无积压时只有几毫秒，与现场 4~10s
    无关。旧实现混淆的正是这两者，所以这里把定义钉死 ——
    render_age_ms 必须包含收包侧的积压，否则 4000ms 的排队延迟根本读不出来。
    """

    def __init__(self, relay_port: int, hold_ms: float) -> None:
        super().__init__(daemon=True, name="viewer-stub")
        self._relay = relay_port
        self._hold_s = hold_ms / 1000.0
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self._target = ("127.0.0.1", relay_port)
        # 积压队列：(首包到达时刻, 字节数, AU 完整时刻)
        self._backlog: deque[tuple[float, int, float]] = deque()
        self._lock = threading.Lock()
        self._render_age_ms = -1.0
        self._rendered = 0
        self._peak_backlog_bytes = 0
        self._au_pkts = 0
        self._au_bytes = 0
        self._au_first_at = 0.0
        self._byte_window: deque[tuple[float, int]] = deque(maxlen=4000)
        # 积压字节的短窗序列。只看瞬时值会在"一次调度抖动后批量出队"的
        # 谷底读到接近 0，把 4 秒积压误报成 4ms —— 而积压本来就是估计量，
        # 取短窗峰值才是"队列等待"的稳定读数。
        self._q_hist: deque[tuple[float, int]] = deque(maxlen=2000)

    # relay 用 b"CBR"+kind 学下游地址；PING=0x00 会回 PONG，顺带保活
    # （DOWNSTREAM_STALE_S=10s，超时下游被摘掉就再也收不到画面）。
    def _ping(self) -> None:
        try:
            self.sock.sendto(b"CBR\x00", self._target)
        except OSError:
            pass

    def run(self) -> None:
        self.sock.settimeout(0.2)
        self._ping()
        last_ping = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_ping >= 1.0:
                last_ping = now
                self._ping()
            # 先记积压，再收包、再出队：量的是"本轮开始时还压着多少"，
            # 避免落在批量出队的谷底。
            with self._lock:
                self._q_hist.append(
                    (now, sum(b for _f, b, _c in self._backlog)))
                while self._q_hist and now - self._q_hist[0][0] > 1.5:
                    self._q_hist.popleft()
            try:
                data, _addr = self.sock.recvfrom(4096)
            except (socket.timeout, BlockingIOError):
                data = None
            except OSError:
                break
            if data and data[:3] != b"CBR":
                t = time.monotonic()
                if not self._au_pkts:
                    self._au_first_at = t
                self._au_pkts += 1
                self._au_bytes += len(data)
                self._byte_window.append((t, len(data)))
                if data[1] & 0x80:  # marker -> AU 收尾
                    with self._lock:
                        self._backlog.append(
                            (self._au_first_at, self._au_bytes, t))
                        self._peak_backlog_bytes = max(
                            self._peak_backlog_bytes,
                            sum(b for _f, b, _c in self._backlog))
                    self._au_pkts = 0
                    self._au_bytes = 0
            # 到期的 AU 才"渲染"：这就是注入的排队延迟
            now = time.monotonic()
            with self._lock:
                due = None
                while self._backlog and now >= self._backlog[0][2] + self._hold_s:
                    due = self._backlog.popleft()
            if due is not None:
                first_at, _nbytes, _complete_at = due
                self._render_age_ms = (time.monotonic() - first_at) * 1000.0
                self._rendered += 1

    def stop(self) -> None:
        self._stop.set()

    def _bitrate_bps(self) -> float:
        now = time.monotonic()
        win = [(t, b) for t, b in self._byte_window if now - t <= 3.0]
        if len(win) < 20:
            return 0.0
        span = max(1e-3, win[-1][0] - win[0][0])
        return sum(b for _t, b in win) * 8.0 / span

    def stats(self) -> dict:
        """Agent A 契约键名。LatencyMonitor 只认这些键。"""
        with self._lock:
            backlog_bytes = sum(b for _f, b, _c in self._backlog)
            backlog_pkts = len(self._backlog) * PKTS_PER_AU
        br = self._bitrate_bps()
        with self._lock:
            q_peak = max((b for _t, b in self._q_hist), default=0)
        return {
            "render_age_ms": round(self._render_age_ms, 1),
            # bytes*8 -> bit；br 是 bit/s，所以结果是**秒**，要乘 1000 才是 ms。
            # （server 侧 rcvbuf_max_queue_ms 用的是 kbit/s，故那里不乘。）
            "queue_ms": round(q_peak * 8.0 / br * 1000.0, 1) if br > 0 else -1.0,
            "backlog_packets": backlog_pkts,
            "backlog_bytes": backlog_bytes,
            "drops": 0,
            "loop_us_avg": 0.0,
            "loop_us_max": 0.0,
            # 非契约字段，仅本测试打印用
            "_rendered": self._rendered,
            "_queue_peak_bytes": q_peak,
            "_peak_backlog_bytes": self._peak_backlog_bytes,
            "_bitrate_bps": round(br, 1),
        }


# ---------------------------------------------------------------- HTTP 侧

def _json_body(resp) -> dict:
    import json
    return json.loads(bytes(resp.body).decode("utf-8"))


async def _timing_body() -> dict:
    # n 必须显式传：FastAPI 的 Query(default=60) 只是声明，直接 await 路由
    # 协程时拿到的是 Query 对象，跟 int 比较会 TypeError。
    return _json_body(await video_router.timing(n=60))


async def _status_body() -> dict:
    return _json_body(await video_router.status())


async def _rtp_status_body() -> dict:
    return _json_body(await video_router.rtp_status())


def _make_client(state: dict):
    """把 HTTP 调用接到真实的路由协程上（不起真服务、不占端口）。

    用 httpx.MockTransport，但 handler 直接 await 真实的路由函数，所以测的
    是 server/routers/video.py 的真实实现，而不是把逻辑复制一遍。
    """
    import httpx
    from starlette.requests import Request

    async def _post_report(body: bytes) -> dict:
        scope = {
            "type": "http", "method": "POST",
            "path": "/video/timing/client",
            "headers": [(b"content-type", b"application/json")],
        }

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        return _json_body(
            await video_router.timing_client_report(Request(scope, receive)))

    def handler(request: "httpx.Request") -> "httpx.Response":
        path = request.url.path
        if request.method == "POST" and path == "/video/timing/client":
            state["posts"] = state.get("posts", 0) + 1
            return httpx.Response(
                200, json=asyncio.run(_post_report(request.content or b"{}")))
        if path == "/video/status":
            return httpx.Response(200, json=asyncio.run(_status_body()))
        if path == "/video/rtp_status":
            return httpx.Response(200, json=asyncio.run(_rtp_status_body()))
        if path == "/video/timing":
            return httpx.Response(200, json=asyncio.run(_timing_body()))
        return httpx.Response(404, json={"error": path})

    state.setdefault("posts", 0)
    return httpx.Client(transport=httpx.MockTransport(handler),
                        timeout=3.0, trust_env=False)


async def _post_raw(payload) -> dict:
    """直接调上报协程（用于传坏 body）。"""
    import json as _json
    from starlette.requests import Request
    raw = _json.dumps(payload).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {"type": "http", "method": "POST",
             "path": "/video/timing/client",
             "headers": [(b"content-type", b"application/json")]}
    return _json_body(
        await video_router.timing_client_report(Request(scope, receive)))


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str, detail: str = "") -> None:
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            failures.append(label)

    print("=" * 72)
    print("延迟显示口径对比测试")
    print("=" * 72)

    relay = RtpRelay(RTP_PORT)
    if not relay.start():
        print(f"FAIL: relay :{RTP_PORT} 启动失败")
        return 1

    viewer = _ViewerStub(RTP_PORT, INJECT_HOLD_MS)
    viewer.start()
    src = _Source(RTP_PORT)
    src.start()

    # 等注入的 4000ms 过去，攒够几个真实读数
    warmup = INJECT_HOLD_MS / 1000.0 + 2.5
    print(f"\n注入桌面端排队延迟 {INJECT_HOLD_MS:.0f}ms，"
          f"等待 {warmup:.1f}s 攒样本...")
    time.sleep(warmup)

    relay_st = relay.status()
    stale_ms = float(relay_st.get("staleness_ms", -1))
    vs = viewer.stats()
    render_ms = float(vs["render_age_ms"])
    queue_ms = float(vs["queue_ms"])

    print("\n--- 并排实测：同一条链路，同一时刻 ---")
    print(f"  旧口径 staleness_ms  = {stale_ms:9.1f} ms   "
          f"(relay 侧: 最新帧到 server 有多久; 满流恒 ~33)")
    print(f"  新口径 render_age_ms = {render_ms:9.1f} ms   "
          f"(桌面侧: 我看到的画面有多旧)")
    print(f"  注入 queue_ms        = {queue_ms:9.1f} ms   "
          f"(积压字节 / 实测码率)")
    print(f"  注入 holds           = {INJECT_HOLD_MS:9.1f} ms")
    print(f"  两者差值            = {render_ms - stale_ms:9.1f} ms   "
          f"<- 旧口径完全看不见的延迟")
    print(f"  辅助: 已渲染 {vs['_rendered']} 帧, 峰值积压 "
          f"{vs['_peak_backlog_bytes'] / 1024:.0f} KiB, 实测码率 "
          f"{vs['_bitrate_bps'] / 1000:.0f} kbps, K230 发出 {src.sent} 帧")

    # 必须在任何一次 LatencyMonitor 采样之前取：monitor 会限频往
    # /video/timing/client 上报快照，一旦采过样，"从未上报"这个状态就被
    # 自己覆盖掉了。
    t_fresh = asyncio.run(_timing_body())

    print("\n[1] 旧口径测不出（复现问题）")
    check(0 <= stale_ms <= STALENESS_MAX_MS,
          f"staleness_ms 读数仍约等于一个帧间隔(<= {STALENESS_MAX_MS:.0f}ms)",
          f"{stale_ms:.1f}ms")
    check(relay_st.get("live") is True, "relay 判定链路有流",
          f"live={relay_st.get('live')}")

    print("\n[2] 新口径测得出（修复生效）")
    check(RENDER_AGE_MIN_MS <= render_ms <= RENDER_AGE_MAX_MS,
          f"render_age_ms 读出注入的 {INJECT_HOLD_MS:.0f}ms 积压",
          f"{render_ms:.1f}ms")
    check(render_ms - stale_ms > 3000,
          "render_age_ms 与 staleness_ms 拉开 >3s（零延迟链路下不可能）",
          f"差 {render_ms - stale_ms:.1f}ms")
    check(queue_ms > 1000, "queue_ms 反映积压（>1s）", f"{queue_ms:.1f}ms")

    print("\n[3] LatencyMonitor 把新口径接到面板数据源")
    state: dict = {}
    client = _make_client(state)
    mon = LatencyMonitor(get_base_url=lambda: "http://server")
    mon.set_stream_stats_provider(viewer.stats)
    mon._sample_once(client)
    st = mon.stats()
    print(f"  stats(): current_ms={st['current_ms']} "
          f"latency_source={st['latency_source']} "
          f"staleness_ms={st['staleness_ms']} local_ms={st['local_ms']} "
          f"queue_ms={st['queue_ms']} backlog_packets={st['backlog_packets']} "
          f"drops={st['drops']}")
    check(st["latency_source"] == "render_age", "主延迟源标记为 render_age")
    check(st["current_ms"] >= RENDER_AGE_MIN_MS, "面板主数字读出注入的积压",
          f"{st['current_ms']}ms")
    check(st["staleness_ms"] <= STALENESS_MAX_MS,
          "同一采样里 staleness 仍 ~33ms（证明两者互相独立）",
          f"{st['staleness_ms']}ms")
    check(st["latency_available"] is True, "latency_available=True")
    check(st["p50_ms"] >= RENDER_AGE_MIN_MS, "p50 统计的是真实滞后",
          f"{st['p50_ms']}ms")
    for key in PANEL_KEYS:
        check(key in st, f"stats() 含面板所需键 {key}")
    check(0 <= st["local_ms"] < 100,
          "local_ms(本机往返+取帧)确实是本机量级，与真实滞后区分开",
          f"{st['local_ms']}ms")

    print("\n[4] 优雅降级：Agent A 字段缺失/坏值不得崩溃")
    mon2 = LatencyMonitor(get_base_url=lambda: "http://server")
    mon2._sample_once(client)
    st2 = mon2.stats()
    check(st2["current_ms"] == -1, "完全无上报时主数字为 -1（面板显示 —）",
          str(st2["current_ms"]))
    check(st2["latency_source"] == "unavailable", "标记 unavailable")
    check(st2["latency_available"] is False, "latency_available=False")
    check(0 <= st2["local_ms"] < 100, "本机往返仍然可用",
          f"{st2['local_ms']}ms")

    for label, bad in (
        ("None", None),
        ("空 dict", {}),
        ("全 None 值", {k: None for k in STREAM_METRIC_KEYS}),
        ("字符串值", {k: "abc" for k in STREAM_METRIC_KEYS}),
        ("NaN/负数", {"render_age_ms": float("nan"), "queue_ms": -5.0,
                      "drops": -1}),
        ("混合缺失", {"render_age_ms": 4100.0, "backlog_packets": 90}),
        ("多余未知键", {"render_age_ms": 4100.0, "totally_unknown": object()}),
    ):
        try:
            mon2.report_stream_stats(bad)
            mon2._sample_once(client)
            ok = isinstance(mon2.stats()["current_ms"], (int, float))
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"    异常: {e!r}")
        check(ok, f"viewer 上报 {label} 不抛异常")

    mon2.report_stream_stats({"render_age_ms": 4100.0})
    mon2._sample_once(client)
    stx = mon2.stats()
    check(stx["current_ms"] == 4100.0,
          "只上报 render_age_ms 时其余字段降级为 -1 而非 KeyError",
          f"drops={stx['drops']} queue_ms={stx['queue_ms']} "
          f"loop_us_max={stx['loop_us_max']}")

    print("\n[5] /video/timing 透出新字段")
    t0 = t_fresh
    for key in ("render_age_ms", "queue_ms", "backlog_packets", "backlog_bytes",
                "drops", "loop_us_avg", "loop_us_max", "kbps", "pps",
                "rcvbuf_bytes", "rcvbuf_max_queue_ms", "client_report_age_ms",
                "staleness_ms"):
        check(key in t0, f"/video/timing 含 {key}")
    check(t0["render_age_ms"] is None,
          "从未上报时 render_age_ms 为 null（不是 0、不回退成 staleness）",
          f"staleness_ms={t0['staleness_ms']}")
    check(t0["client_reported_at"] is None,
          "从未上报时 client_reported_at 为 null（不伪造时间戳）")
    check(t0["rcvbuf_bytes"] == 256 * 1024, "端点报告收紧后的缓冲",
          f"{t0['rcvbuf_bytes']} B")

    mon._last_client_report = 0.0   # 强制一次上报
    mon._sample_once(client)
    t1 = asyncio.run(_timing_body())
    check(t1["render_age_ms"] is not None
          and t1["render_age_ms"] >= RENDER_AGE_MIN_MS,
          "上报后 /video/timing 透出真实滞后", f"{t1['render_age_ms']}ms")
    check(t1["client_report_age_ms"] is not None
          and t1["client_report_age_ms"] >= 0,
          "给出上报快照自身的新鲜度", f"{t1['client_report_age_ms']}ms")
    check(state["posts"] >= 1, "LatencyMonitor 确实推了一次上报",
          f"{state['posts']} 次")

    r = asyncio.run(_post_raw({"render_age_ms": "abc", "queue_ms": -1,
                               "drops": None, "backlog_bytes": 12345}))
    check(r.get("ok") is True, "坏值上报被丢弃但仍返回 200", str(r))
    t2 = asyncio.run(_timing_body())
    check(t2["render_age_ms"] is None and t2["backlog_bytes"] == 12345.0,
          "坏值丢成 None、好值原样保留",
          f"render_age={t2['render_age_ms']} backlog={t2['backlog_bytes']}")

    print("\n[6] SO_RCVBUF 延迟上界推导（server 实测常量）")
    rcvbuf = rtp_relay.SO_RCVBUF_BYTES
    kbps = float(relay_st.get("kbps") or 0)
    print(f"  SO_RCVBUF_BYTES       = {rcvbuf} B ({rcvbuf / 1024:.0f} KiB)")
    print(f"  旧 rtp_relay 4 MiB   / 3Mbps = {4 * 1024 * 1024 * 8 / 3e6:6.2f} s")
    print(f"  旧 video.py  1 MiB   / 3Mbps = {1 * 1024 * 1024 * 8 / 3e6:6.2f} s")
    print(f"  新 {rcvbuf / 1024:.0f} KiB       / 3Mbps = "
          f"{rcvbuf * 8 / 3e6:6.3f} s")
    check(rcvbuf == 256 * 1024, "SO_RCVBUF 已收到 256KiB", f"{rcvbuf} B")
    check(rcvbuf * 8 / 3e6 < 0.75, "延迟上界 < 750ms",
          f"{rcvbuf * 8 / 3e6:.3f}s")
    if kbps > 0:
        print(f"  按本测试实测 {kbps:.0f}kbps 折算上界 = "
              f"{rcvbuf * 8 / (kbps * 1000) * 1000:.0f}ms")
        print(f"  relay status.rcvbuf_max_queue_ms = "
              f"{relay_st.get('rcvbuf_max_queue_ms')}")

    print("\n[7] 真实 H264Viewer.stats() 的契约覆盖（供 Agent A 联调参考）")
    try:
        from desktop.streaming.h264_viewer import H264Viewer
        real = set(H264Viewer("http://127.0.0.1:1").stats().keys())
        missing = [k for k in STREAM_METRIC_KEYS if k not in real]
        if missing:
            print(f"  尚未产出: {missing}")
            print("  -> 本测试用 _ViewerStub 契约替身; Agent A 落地后"
                  "本测试对延迟口径的断言对真实实现同样成立。")
        else:
            print("  契约字段全部已产出，可直接联调。")
    except Exception as e:  # noqa: BLE001
        print(f"  无法检查: {e!r}")

    client.close()
    src.stop()
    viewer.stop()
    relay.stop()

    print("\n" + "=" * 72)
    if failures:
        print(f"FAIL -- {len(failures)} 项未通过:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS -- 旧口径 ~33ms 测不出，新口径读出注入的积压")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())