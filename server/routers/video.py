"""UDP 监听 + HTTP 输出。

UDP 端口默认 8001，可用环境变量 CORE_BRIDGE_VIDEO_UDP_PORT 覆盖。
HTTP：
  GET /video/status      状态/统计（desktop 轮询健康度）
  GET /video/latest.jpg  最新完整帧；?since=<frame_id> 若无新帧返回 304
  GET /video/mjpeg       multipart MJPEG（浏览器调试用，非桌面主链路）
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from server import command_udp
from server.rtp_relay import relay_status
from server.rtp_relay import SO_RCVBUF_BYTES
from server.video_hub import hub

logger = logging.getLogger("video_udp")

router = APIRouter(prefix="/video", tags=["video"])

UDP_PORT = int(os.environ.get("CORE_BRIDGE_VIDEO_UDP_PORT", "8001"))
UDP_HOST = os.environ.get("CORE_BRIDGE_VIDEO_UDP_HOST", "0.0.0.0")

# 内核接收缓冲就是这条链路的延迟上界：上界 = 缓冲字节 / 码率。
# 3Mbps（375,000 B/s）下旧值 1 MiB -> 2.80 s，256 KiB -> 0.70 s。
# 缓冲越大，"丢一帧"越容易变成"停在几秒前的旧画面"，而到达陈旧度依然
# 读 33ms，看板上完全看不出来。与 RTP 链路用同一个常量，两条链路的
# 延迟上界才可比。

_transport: asyncio.DatagramTransport | None = None

# 桌面端上报的本机指标（见 POST /video/timing/client）。这些量产生在桌面
# 进程里，server 自己测不出，所以只能由上报获得；从未上报时是 None ——
# **不填 0，也不回退成 staleness_ms**，那正是 4227d80 之前"显示在骗人"的
# 根因：把一个与现场无关的数（33ms 的到达陈旧度）当成延迟往外送。
CLIENT_TIMING_KEYS = (
    "render_age_ms",
    "queue_ms",
    "backlog_packets",
    "backlog_bytes",
    "drops",
    "loop_us_avg",
    "loop_us_max",
)
_client_timing: dict[str, float | None] = {k: None for k in CLIENT_TIMING_KEYS}
_client_timing_at: float = 0.0
_client_timing_recv: int = 0


def _coerce_ms(v: object) -> float | None:
    """上报值安全转 float；缺失/None/NaN/负数一律 None（优雅降级）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if x != x or x < 0:  # NaN 或负数无意义
        return None
    return round(x, 1)


def _client_timing_payload() -> dict[str, float | None]:
    """上报字段的对外视图。没有上报时全部 None，而不是缺键。"""
    return {k: _client_timing.get(k) for k in CLIENT_TIMING_KEYS}


class _VideoProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        try:
            host = addr[0] if isinstance(addr, tuple) else str(addr)
            port = int(addr[1]) if isinstance(addr, tuple) else 0
        except Exception:
            host, port = "", 0
        valid = hub.feed_datagram(data, host)
        # 学习视频发送地址，作为命令反向推送目标（头部校验通过即算）
        if valid and port:
            command_udp.note_video_sender(host, port)


async def start_udp_listener(host: str = UDP_HOST, port: int = UDP_PORT) -> None:
    """在 server lifespan 中调用；端口被占则记错但不让 HTTP 挂掉。"""
    global _transport
    if _transport is not None:
        return
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            _VideoProtocol,
            local_addr=(host, port),
        )
        # 内核收包缓冲刻意压到 256KiB：缓冲越大越能把丢包换成排队延迟，
        # 上界 = 字节 / 码率（见 SO_RCVBUF_BYTES 注释）。
        sock_obj = None
        try:
            sock_obj = transport.get_extra_info("socket")
            if isinstance(sock_obj, socket.socket):
                sock_obj.setsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF, SO_RCVBUF_BYTES)
        except Exception:
            pass
        _transport = transport
        logger.info("video UDP listening on %s:%d", host, port)
        # 命令反向推送复用同一视频 socket：目标地址由收到的视频分片学习
        # （note_video_sender），无需 K230 注册；取底层 socket.sendto
        # （py3.14 的 get_extra_info 返回 TransportSocket 包装，需剥出 _sock）
        raw_sock = getattr(sock_obj, "_sock", sock_obj)
        command_udp.start_command_udp(sendto=raw_sock.sendto)
    except OSError as e:
        logger.error("video UDP bind %s:%d failed: %s", host, port, e)


async def stop_udp_listener() -> None:
    global _transport
    command_udp.stop_command_udp()
    t, _transport = _transport, None
    if t is not None:
        t.close()


@router.get("/status")
async def status() -> JSONResponse:
    return JSONResponse(hub.status(UDP_PORT))


@router.get("/rtp_status")
async def rtp_status() -> JSONResponse:
    """H264 裸 RTP 中转状态（server/rtp_relay.py，udp:8002）。"""
    return JSONResponse(relay_status())


@router.post("/timing/client")
async def timing_client_report(request: Request) -> JSONResponse:
    """桌面端上报它**本机**测到的链路指标（Agent A 的 viewer.stats()）。

    为什么需要这一跳：render_age_ms / queue_ms / backlog_* / drops 全都产生在
    桌面进程里（AU 到达 -> 渲染完成），server 无从得知。但把这份快照记到
    server 上是有价值的 —— 现场排障时对着 server 就能读到"操作手当时看到的
    画面有多旧"，而不必去操作手那台机器上开面板。上一版恰恰做不到这一点：
    server 只有 staleness_ms，压 4s 照样读 33ms。

    契约：body 是任意 dict，**未知键忽略、坏值丢弃、缺键留 None**。任何
    异常都不返回 5xx —— 上报失败不该影响链路或面板。
    """
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return JSONResponse({"ok": True, "accepted": 0})
    clean: dict[str, float | None] = {}
    for key in CLIENT_TIMING_KEYS:
        clean[key] = _coerce_ms(payload.get(key))
    global _client_timing, _client_timing_at, _client_timing_recv
    _client_timing = clean
    _client_timing_at = time.time()
    _client_timing_recv = sum(1 for v in clean.values() if v is not None)
    return JSONResponse({"ok": True, "accepted": _client_timing_recv})


@router.get("/timing")
async def timing(n: int = Query(default=60, ge=5, le=120)) -> JSONResponse:
    """到达节奏诊断 + 延迟上界（原 /video/latency）。

    三组数，语义互不重叠，看的时候别混：

    1. 到达节奏：frame_id / fps / jitter / intervals_ms / staleness_ms。
       staleness_ms 是"最后一帧到 server 有多久"，**不是延迟**：新帧一直在
       到，它满流时恒在 [0,33]ms，压 4s 延迟也读 33ms。只当停流旁证。
    2. 延迟上界：kbps + rcvbuf_bytes + rcvbuf_max_queue_ms。
       后者 = 缓冲字节*8/码率，是"包已到达但排在 socket 缓冲里没被读走"
       能贡献的最坏秒数。延迟异常时先看它区分"空中段慢"还是"缓冲里排队"。
    3. 桌面侧真实指标：render_age_ms / queue_ms / backlog_* / drops，
       来自 POST /video/timing/client 的上报。**server 自己测不出**，从未
       上报过时为 null（不是 0，也不是回退成 staleness —— 那正是 4227d80
       之前的自欺）。client_report_age_ms 告诉你这份快照有多旧。
    """
    st = hub.status(UDP_PORT)
    rs = relay_status()
    now = time.time()
    payload: dict = {
        # --- 1) 到达节奏（非延迟）---
        "live": st["live"],
        "frame_id": st["frame_id"],
        "staleness_ms": st["staleness_ms"],
        "fps": st["fps"],
        "jitter_ms": st["jitter_ms"],
        "intervals_ms": hub.recent_intervals_ms(n),
        # --- 2) 延迟上界（server 可测）---
        "kbps": rs.get("kbps", 0.0),
        "pps": rs.get("pps", 0.0),
        "rcvbuf_bytes": rs.get("rcvbuf_bytes", SO_RCVBUF_BYTES),
        "rcvbuf_max_queue_ms": rs.get("rcvbuf_max_queue_ms", -1.0),
        "rtp_live": rs.get("live", False),
        "upstream": rs.get("upstream"),
        "downstreams": rs.get("downstreams", 0),
        # --- 3) 桌面侧真实指标（未上报则 null，绝不伪造）---
        "server_time": st["server_time"],
        "client_reported_at": _client_timing_at or None,
        "client_report_age_ms": round((now - _client_timing_at) * 1000.0, 1)
        if _client_timing_at else None,
        "client_fields": _client_timing_recv,
    }
    payload.update(_client_timing_payload())
    return JSONResponse(payload)


@router.get("/latest.jpg")
async def latest(since: int | None = Query(default=None)) -> Response:
    if hub.latest_jpeg is None:
        return Response(status_code=404, content="no frame yet")
    if since is not None and since == hub.latest_frame_id:
        return Response(status_code=304)
    return Response(
        content=hub.latest_jpeg,
        media_type="image/jpeg",
        headers={
            "X-Frame-Id": str(hub.latest_frame_id),
            # 陈旧度不是延迟，名字必须说清楚，否则客户端会拿它当管线延迟
            "X-Frame-Staleness-Ms": str(hub.staleness_ms()),
            "Cache-Control": "no-store",
        },
    )


@router.get("/mjpeg")
async def mjpeg(fps: int = Query(default=10, ge=1, le=30)):
    boundary = "frame"
    interval = 1.0 / fps

    async def gen():
        last_id = -1
        while True:
            if hub.latest_jpeg is not None and hub.latest_frame_id != last_id:
                last_id = hub.latest_frame_id
                chunk = (
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(hub.latest_jpeg)}\r\n"
                    f"X-Frame-Id: {last_id}\r\n\r\n"
                ).encode() + hub.latest_jpeg + b"\r\n"
                yield chunk
            await asyncio.sleep(interval)

    return StreamingResponse(
        gen(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-store"},
    )
